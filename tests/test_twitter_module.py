"""Preserved archive bounds, fresh guards, module integration and exact bytes."""
from contextlib import contextmanager
import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import zipfile
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
import config
import database
from files_base import index, sources, serving
from modules import twitter
from modules.twitter import archive, router, jobs, acquisition, helpers
from tests.browser_twitter_fixture import RAW_TEXT, write_capture, source_response


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    media = tmp_path / 'posts'
    media.mkdir()
    user, db = home / 'user.sqlite', home / 'base/files.sqlite'

    @contextmanager
    def connect():
        connection = sqlite3.connect(user)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()
    with connect() as connection:
        sources.ensure_sources_schema(connection)
        twitter.publish(connection)
        source = sources.register_source(connection, media, 'Saved posts')
    monkeypatch.setattr(router, 'get_user_db', connect)
    monkeypatch.setattr(database, 'get_user_db', connect)
    monkeypatch.setattr(jobs, 'get_user_db', connect)
    monkeypatch.setattr(config, 'USER_DB_PATH', user)
    monkeypatch.setattr(config, 'FILES_DB_PATH', db)
    monkeypatch.setattr(config, 'SUITE_HOME', home)
    return source, media, db, connect


def scan(catalog):
    source, media, db, _ = catalog
    with index.open_index(db) as connection:
        index.scan_source(connection, source.source_id, media)


def browse(offset=0, limit=120, query='', author=''):
    return router.library(offset, limit, query, author)


def rewrite(path, change):
    value = json.loads(path.read_text(encoding='utf-8'))
    change(value)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def test_role_release_and_forget_preserve_every_original(catalog):
    source, media, _, connect = catalog
    path = write_capture(media)
    before = {p: p.read_bytes() for p in media.rglob('*') if p.is_file()}
    scan(catalog)
    assert browse()['items'] == []
    with pytest.raises(HTTPException) as error:
        router.capture(source.source_id, path)
    assert error.value.status_code == 404
    twitter.adopt(source.source_id)
    assert router.folders()[0]['available']
    assert browse()['items'][0]['raw_text'] == RAW_TEXT
    twitter.release(source.source_id)
    assert browse()['items'] == []
    twitter.adopt(source.source_id)
    twitter.release(source.source_id, True)
    with connect() as connection:
        assert sources.get_source(connection, source.source_id) is None
    assert before == {p: p.read_bytes() for p in before}
    with pytest.raises(ValueError):
        twitter.adopt(source.source_id)


def test_newest_multi_source_pagination_duplicates_and_exact_text(catalog):
    source, media, db, connect = catalog
    for number in range(9):
        write_capture(media, number, post_id='333', author_id=None if number == 8 else '123')
    scan(catalog)
    twitter.adopt(source.source_id)
    sibling = media.parent / 'other'
    sibling.mkdir()
    same_path = write_capture(sibling, 8, post_id='333')
    with connect() as connection:
        second = sources.register_source(connection, sibling, 'Saved posts')
    twitter.adopt(second.source_id)
    with index.open_index(db) as connection:
        index.scan_source(connection, second.source_id, sibling)
    result = browse()
    assert result['total'] == 10 and result['next_offset'] is None
    assert len(result['items']) == 10
    assert {r['source_id'] for r in result['items'] if r['path'] == same_path} == {source.source_id, second.source_id}
    assert all(r['raw_text'] == RAW_TEXT and r['available'] for r in result['items'])
    assert [r['capture_id'] for r in result['items']] == sorted((r['capture_id'] for r in result['items']), reverse=True)
    assert [r for offset in range(0, 10, 3) for r in browse(offset, 3)['items']] == result['items']
    assert browse(999, 3)['next_offset'] is None
    assert len(browse(query='이름')['items']) == 10
    assert browse(query='%_')['items'] == []  # Literal, not SQL wildcards.
    assert len(browse(author='id:123')['items']) == 9
    assert len(browse(author='handle:savedauthor')['items']) == 1
    with connect() as connection:
        sources.update_source(connection, second.source_id, visible=False)
    assert browse()['total'] == 9


def test_filtered_and_invalid_pages_advance_cursor(catalog):
    source, media, _, _ = catalog
    paths = [write_capture(media, n, text='match' if n == 0 else 'different') for n in range(4)]
    rewrite(media / paths[3], lambda m: m.update(complete=False))
    scan(catalog)
    twitter.adopt(source.source_id)
    page = browse(0, 2, 'match')
    assert page == {'items': [], 'total': 4, 'next_offset': 2, 'invalid': 1}
    next_page = browse(page['next_offset'], 2, 'match')
    assert len(next_page['items']) == 1 and next_page['next_offset'] is None


@pytest.mark.parametrize('change', [
    lambda m: m.update(schema_version=True), lambda m: m.update(complete=False),
    lambda m: m.update(post_id='999'), lambda m: m.update(capture_id='wrong'),
    lambda m: m.update(captured_at='2026-10-11T13:00:00Z'),
    lambda m: m.update(completed_at='2026-10-01T12:00:00Z'),
    lambda m: m.update(source_url='https://example.com/author/status/2000000'),
    lambda m: m['files'].update({'../outside': m['files']['video.mp4']}),
    lambda m: m['files']['tweet.json'].update(sha256='0' * 64),
    lambda m: m['files']['video.mp4'].update(size=True),
])
def test_hostile_manifests_never_enter_feed(catalog, change):
    source, media, _, _ = catalog
    path = write_capture(media)
    rewrite(media / path, change)
    scan(catalog)
    twitter.adopt(source.source_id)
    assert browse()['items'] == [] and browse()['invalid'] == 1
    with pytest.raises(HTTPException) as error:
        router.capture(source.source_id, path)
    assert error.value.status_code == 422


@pytest.mark.parametrize('path', ['../outside', '/outside', 'C:\\outside', 'captures/1/wrong/manifest.json',
                                 'captures/1/20261311T120000000000Z-deadbeef/manifest.json'])
def test_format_paths_refused_before_disk_reads(catalog, path):
    source, _, _, _ = catalog
    twitter.adopt(source.source_id)
    with patch.object(Path, 'open', side_effect=AssertionError('Hostile path must not be read')):
        with pytest.raises(HTTPException) as error:
            router.capture(source.source_id, path)
    assert error.value.status_code == 422


def test_missing_media_remains_a_saved_unavailable_snapshot(catalog):
    source, media, _, _ = catalog
    path = write_capture(media)
    scan(catalog)
    twitter.adopt(source.source_id)
    video = media / path.replace('manifest.json', 'video.mp4')
    video.rename(video.with_suffix('.mp4.preserved'))
    result = router.capture(source.source_id, path)
    assert result['available'] is False and result['unavailable_assets'] == ['video.mp4']
    assert browse()['items'][0]['raw_text'] == RAW_TEXT


def test_nested_hidden_and_private_ownership_is_fresh(catalog):
    source, media, _, connect = catalog
    path = write_capture(media)
    scan(catalog)
    twitter.adopt(source.source_id)
    with connect() as connection:
        child = sources.register_source(connection, media / 'captures', 'Separate owner')
        sources.update_source(connection, child.source_id, visible=False)
    assert browse()['items'] == []
    with pytest.raises(HTTPException) as error:
        router.capture(source.source_id, path)
    assert error.value.status_code == 403
    with connect() as connection:
        sources.update_source(connection, source.source_id, visible=False)
    assert router.folders() == [] and browse()['total'] == 0
    with connect() as connection:
        private = sources.register_source(connection, config.SUITE_HOME, 'Private')
    twitter.adopt(private.source_id)
    assert router.folders()[0]['available'] is False


def test_symlink_metadata_escape_never_reads_outside(catalog, tmp_path):
    source, media, _, _ = catalog
    path = write_capture(media)
    outside = tmp_path / 'outside.json'
    outside.write_bytes((media / path).read_bytes())
    (media / path).rename((media / path).with_suffix('.json.preserved'))
    (media / path).symlink_to(outside)
    scan(catalog)
    twitter.adopt(source.source_id)
    opened = []
    original = Path.open
    def check(target, *args, **kwargs):
        opened.append(target)
        assert target != outside
        return original(target, *args, **kwargs)
    with patch.object(Path, 'open', check):
        assert browse()['items'] == []
        with pytest.raises(HTTPException) as error:
            router.capture(source.source_id, path)
    assert error.value.status_code == 403 and outside not in opened


def test_metadata_hash_and_read_identity_fail_closed(catalog):
    source, media, _, _ = catalog
    path = write_capture(media)
    scan(catalog)
    twitter.adopt(source.source_id)
    original = Path.open
    @contextmanager
    def changed(target, *args, **kwargs):
        with original(target, *args, **kwargs) as handle:
            class MutatingRead:
                def fileno(self):
                    return handle.fileno()
                def read(self, maximum):
                    raw = handle.read(maximum)
                    with original(target, 'ab') as writer:
                        writer.write(b' ')
                    return raw
            yield MutatingRead()
    with patch.object(Path, 'open', changed):
        with pytest.raises(HTTPException) as error:
            router.capture(source.source_id, path)
    assert error.value.status_code == 422
    tweet = media / path.replace('manifest.json', 'tweet.json')
    rewrite(tweet, lambda m: m.update(raw_text='changed'))
    assert browse()['items'] == []


def test_page_bytes_bounded_and_media_bodies_never_read(catalog):
    source, media, _, _ = catalog
    for number in range(8):
        write_capture(media, number, text='한' * 200000)
    scan(catalog)
    twitter.adopt(source.source_id)
    original_open = Path.open
    opened = []
    def check(path, *args, **kwargs):
        assert path.name in ('manifest.json', 'tweet.json')
        opened.append(path)
        return original_open(path, *args, **kwargs)
    with patch.object(Path, 'open', check):
        first = browse()
        assert 0 < len(first['items']) < 8 and first['next_offset'] == len(first['items'])
        second = browse(first['next_offset'])
    assert len(first['items']) + len(second['items']) == 8
    assert len({r['path'] for r in first['items'] + second['items']}) == 8
    assert all(p.name != 'source.json' for p in opened)


def test_oversized_and_deeply_nested_metadata_is_bounded(catalog):
    source, media, _, _ = catalog
    paths = [write_capture(media, n) for n in range(2)]
    (media / paths[0]).write_bytes(b' ' * (archive.MANIFEST_LIMIT + 1))
    (media / paths[1]).write_bytes(b'[' * 5000 + b']' * 5000)
    scan(catalog)
    twitter.adopt(source.source_id)
    assert browse()['invalid'] == 2


def test_duplicate_metadata_keys_are_refused(catalog):
    source, media, _, _ = catalog
    path = write_capture(media)
    data = (media / path).read_bytes()
    (media / path).write_bytes(data.replace(b'"complete": true', b'"complete": false, "complete": true'))
    scan(catalog)
    twitter.adopt(source.source_id)
    assert browse()['items'] == [] and browse()['invalid'] == 1


@pytest.mark.parametrize('change', [
    lambda m: m.update(raw_text=None), lambda m: m.update(raw_text='\ud800'),
    lambda m: m['author'].update(id=123), lambda m: m['author'].update(handle='../outside'),
    lambda m: m.update(posted_at='2026-10-10T12:00:00'),
])
def test_invalid_tweet_fields_refused_after_correct_hash(catalog, change):
    source, media, _, _ = catalog
    path = write_capture(media)
    target = media / path.replace('manifest.json', 'tweet.json')
    value = json.loads(target.read_text(encoding='utf-8'))
    change(value)
    raw = json.dumps(value).encode()
    target.write_bytes(raw)
    rewrite(media / path, lambda m: m['files']['tweet.json'].update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest()))
    scan(catalog)
    twitter.adopt(source.source_id)
    assert browse()['items'] == [] and browse()['invalid'] == 1


def test_media_rechecks_role_and_visibility_after_metadata(catalog):
    source, media, _, connect = catalog
    path = write_capture(media)
    twitter.adopt(source.source_id)
    original = router.capture
    def hidden(*args):
        item = original(*args)
        with connect() as connection:
            sources.update_source(connection, source.source_id, visible=False)
        return item
    request = Request({'type': 'http', 'headers': []})
    with patch.object(router, 'capture', hidden):
        with pytest.raises(HTTPException) as error:
            router.media(request, source.source_id, path, 'video')
    assert error.value.status_code == 404


def test_media_rechecks_size_and_missing_asset(catalog):
    source, media, _, _ = catalog
    path = write_capture(media)
    twitter.adopt(source.source_id)
    original = router.capture
    video = media / path.replace('manifest.json', 'video.mp4')
    def changed(*args):
        item = original(*args)
        with video.open('ab') as handle:
            handle.write(b'changed')
        return item
    request = Request({'type': 'http', 'headers': []})
    with patch.object(router, 'capture', changed):
        with pytest.raises(HTTPException) as error:
            router.media(request, source.source_id, path, 'video')
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        router.media(request, source.source_id, path, 'video')
    assert error.value.status_code == 404


@pytest.mark.parametrize('url', ['http://x.com/a/status/1', 'https://evil.com/a/status/1',
    'https://x.com.evil.com/a/status/1', 'https://a@x.com/a/status/1',
    'https://x.com:444/a/status/1', 'https://x.com/a/status/0', 'https://x.com/a/status/1/more',
    'https://x.com\\evil.com/a/status/1', 'https://x.com/a/status/1\n',
    'https://x.com/a/status/%31', 'https://x.com/a/status/12345678901234567890123'])
def test_status_url_admission_is_closed(url):
    with pytest.raises(archive.InvalidCapture):
        archive.canonical_url(url)


def test_status_url_normalization():
    assert archive.canonical_url('https://www.twitter.com/SavedAuthor/status/123/?s=20#ignored') == ('https://x.com/SavedAuthor/status/123', '123')
    assert archive.canonical_url('https://x.com/i/web/status/123') == ('https://x.com/i/web/status/123', '123')


@pytest.mark.parametrize('platform,name', [('win32', 'ffmpeg.exe'), ('linux', 'ffmpeg')])
def test_frozen_twitter_uses_bundled_ffmpeg_without_python_wrapper(tmp_path, monkeypatch, platform, name):
    import builtins
    portable = tmp_path / 'portable'
    portable.mkdir()
    executable = portable / 'Keivotos'
    executable.touch()
    bundled = portable / name
    bundled.touch()
    monkeypatch.setattr(helpers.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(helpers.sys, 'platform', platform)
    monkeypatch.setattr(helpers.sys, 'executable', str(executable))
    monkeypatch.setattr(helpers.shutil, 'which', lambda _: pytest.fail('Frozen lookup used PATH'))
    original_import = builtins.__import__
    def without_wrapper(name, *args, **kwargs):
        if name == 'imageio_ffmpeg':
            pytest.fail('Portable lookup imported the excluded Python wrapper')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', without_wrapper)
    monkeypatch.setattr(jobs.importlib.util, 'find_spec', lambda package: None if package == 'imageio_ffmpeg' else object())
    assert helpers.ffmpeg_executable() == str(bundled)
    assert jobs.dependencies() == {'ready': True, 'missing': []}


def test_frozen_twitter_missing_bundle_refuses_ambient_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(helpers.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(helpers.sys, 'executable', str(tmp_path / 'Keivotos'))
    monkeypatch.setattr(helpers.shutil, 'which', lambda _: pytest.fail('Frozen lookup used PATH'))
    monkeypatch.setattr(jobs.importlib.util, 'find_spec', lambda _: object())
    assert helpers.ffmpeg_executable() is None
    assert jobs.dependencies() == {'ready': False, 'missing': ['FFmpeg']}


def test_source_twitter_ffmpeg_uses_path_before_python_wrapper(monkeypatch):
    import builtins
    monkeypatch.setattr(helpers.sys, 'frozen', False, raising=False)
    monkeypatch.setattr(helpers.shutil, 'which', lambda _: '/existing/ffmpeg')
    original_import = builtins.__import__
    def without_wrapper(name, *args, **kwargs):
        if name == 'imageio_ffmpeg':
            pytest.fail('Source PATH lookup unnecessarily imported the wrapper')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', without_wrapper)
    assert helpers.ffmpeg_executable() == '/existing/ffmpeg'


def test_source_twitter_ffmpeg_keeps_installed_wrapper_fallback(monkeypatch):
    import imageio_ffmpeg
    monkeypatch.setattr(helpers.sys, 'frozen', False, raising=False)
    monkeypatch.setattr(helpers.shutil, 'which', lambda _: None)
    monkeypatch.setattr(imageio_ffmpeg, 'get_ffmpeg_exe', lambda: '/locked/environment/ffmpeg')
    assert helpers.ffmpeg_executable() == '/locked/environment/ffmpeg'


def test_source_twitter_reports_missing_ffmpeg_without_path_or_wrapper(monkeypatch):
    import builtins
    monkeypatch.setattr(helpers.sys, 'frozen', False, raising=False)
    monkeypatch.setattr(helpers.shutil, 'which', lambda _: None)
    monkeypatch.setattr(jobs.importlib.util, 'find_spec', lambda _: object())
    original_import = builtins.__import__
    def without_wrapper(name, *args, **kwargs):
        if name == 'imageio_ffmpeg':
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', without_wrapper)
    assert jobs.dependencies() == {'ready': False, 'missing': ['FFmpeg']}


@pytest.mark.parametrize('package', ['yt_dlp', 'PIL'])
def test_twitter_ffmpeg_does_not_mask_other_missing_dependencies(monkeypatch, package):
    monkeypatch.setattr(jobs, 'ffmpeg_executable', lambda: '/existing/ffmpeg')
    monkeypatch.setattr(jobs.importlib.util, 'find_spec', lambda name: None if name == package else object())
    assert jobs.dependencies() == {'ready': False, 'missing': [package]}


@pytest.mark.parametrize('corrupt', [False, True])
def test_real_frozen_twitter_verification_without_imageio_ffmpeg(catalog, tmp_path, monkeypatch, corrupt):
    import builtins
    from io import BytesIO
    import shutil
    from PIL import Image
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    generated = tmp_path / 'generated.mp4'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=blue:s=48x48:r=4',
                    '-t', '0.5', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(generated)], check=True, timeout=30)
    portable = tmp_path / 'portable'
    portable.mkdir()
    executable = portable / ('Keivotos.exe' if sys.platform == 'win32' else 'Keivotos')
    executable.touch()
    bundled = portable / ('ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg')
    shutil.copy2(ffmpeg, bundled)
    avatar = BytesIO()
    Image.new('RGB', (24, 24), 'blue').save(avatar, 'JPEG')
    media = catalog[1]
    path = write_capture(media, video=b'invalid MP4' if corrupt else generated.read_bytes(), avatar=avatar.getvalue())
    directory = (media / path).parent
    before = {file.name: file.read_bytes() for file in directory.iterdir()}
    spec = {'root': str(media.resolve()), 'path': path, 'forbidden': [],
            'directory_identity': [directory.stat().st_dev, directory.stat().st_ino]}
    monkeypatch.setattr(helpers.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(helpers.sys, 'executable', str(executable))
    original_import = builtins.__import__
    original_spec = jobs.importlib.util.find_spec
    def without_wrapper(name, *args, **kwargs):
        if name == 'imageio_ffmpeg':
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', without_wrapper)
    monkeypatch.setattr(jobs.importlib.util, 'find_spec', lambda name: None if name == 'imageio_ffmpeg' else original_spec(name))
    assert jobs.dependencies() == {'ready': True, 'missing': []}
    if corrupt:
        with pytest.raises(ValueError, match='Saved MP4 verification failed'):
            acquisition.verify(spec)
    else:
        verified = acquisition.verify(spec)
        assert set(verified) == set(archive.ASSET_LIMITS)
        for name, facts in verified.items():
            assert facts['size'] == len(before[name])
            assert facts['sha256'] == hashlib.sha256(before[name]).hexdigest()
    assert {file.name: file.read_bytes() for file in directory.iterdir()} == before


def test_failed_twitter_startup_leaves_base_running_and_can_retry(catalog, monkeypatch):
    import lifecycle
    from module_registry import build_registry
    _, _, _, connect = catalog
    monkeypatch.setattr(lifecycle, 'get_user_db', connect)
    registry = build_registry(config.SUITE_HOME, 'fixture')
    def broken():
        raise RuntimeError('Intentional startup failure')
    async def verify():
        runtime = lifecycle.ModuleRuntime()
        assert runtime.start(registry.base)
        descriptor = registry.require('twitter')
        assert runtime.start(replace(descriptor, startup_hook=broken)) is False
        assert runtime.status('twitter')['state'] == 'failed'
        assert runtime.status('files')['state'] == 'running'
        await runtime.stop('twitter')
        assert runtime.start(descriptor)
        assert runtime.status('twitter')['state'] == 'running'
        await runtime.stop('twitter')
        await runtime.stop('files')
    asyncio.run(verify())


def test_index_repair_and_fresh_process_preserve_archive(catalog):
    source, media, db, _ = catalog
    path = write_capture(media)
    twitter.adopt(source.source_id)
    assert browse()['items'] == []
    scan(catalog)
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in media.rglob('*') if p.is_file()}
    code = 'import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from files_base.sources import Source;from modules.twitter.archive import capture;s=Source(sys.argv[2],sys.argv[3],"Saved posts","twitter",True,None);print(capture(s,[s],Path(sys.argv[4]),sys.argv[5])["capture_id"])'
    result = subprocess.run([sys.executable, '-c', code, str(ROOT / 'backend'), source.source_id,
                             str(media), str(config.SUITE_HOME), path], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == browse()['items'][0]['capture_id']
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before}


def test_user_backup_retains_role_without_originals_or_extra_artifact(catalog, tmp_path, monkeypatch):
    import backup_bundle
    source, media, _, _ = catalog
    write_capture(media)
    twitter.adopt(source.source_id)
    components = dict(backup_bundle.COMPONENTS)
    components['user_database'] = ('databases/user.sqlite', config.USER_DB_PATH)
    monkeypatch.setattr(backup_bundle, 'USER_DB_PATH', config.USER_DB_PATH)
    monkeypatch.setattr(backup_bundle, 'COMPONENTS', components)
    selection = {key: key == 'user_database' for key in components}
    monkeypatch.setattr(backup_bundle, 'get_backup_config', lambda: {'destination': str(tmp_path / 'backups'), 'components': selection})
    result = backup_bundle.create_backup_bundle(selection)
    with zipfile.ZipFile(result['path']) as bundle:
        assert not any('captures/' in name or name.endswith(('tweet.json', 'video.mp4', 'avatar.jpg', 'twitter.sqlite')) for name in bundle.namelist())
        snapshot = tmp_path / 'backup-user.sqlite'
        snapshot.write_bytes(bundle.read('databases/user.sqlite'))
    with sqlite3.connect(snapshot) as connection:
        assert connection.execute('SELECT role FROM files_sources WHERE source_id=?', (source.source_id,)).fetchone()[0] == 'twitter'


def job_count(catalog):
    with catalog[3]() as db:
        return db.execute('SELECT COUNT(*) AS count FROM twitter_downloads').fetchone()['count']


def new_job(catalog, token=None):
    source = catalog[0]
    twitter.adopt(source.source_id)
    return jobs.create('https://x.com/SavedAuthor/status/333', source.source_id, token or uuid4())


def test_job_uuid_receipt_duplicate_concurrency_and_durable_queue(catalog):
    from concurrent.futures import ThreadPoolExecutor
    token = uuid4()
    source = catalog[0]
    twitter.adopt(source.source_id)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: new_job(catalog, token), range(4)))
    assert len({r['id'] for r in results}) == 1
    assert job_count(catalog) == 1
    with pytest.raises(ValueError, match='already downloading'):
        new_job(catalog)
    with pytest.raises(ValueError, match='different capture'):
        jobs.create('https://x.com/SavedAuthor/status/444', source.source_id, token)
    jobs.update(str(token), status='complete')
    assert new_job(catalog)['id'] != str(token)
    assert 'root' not in jobs.public(jobs.get(str(token)))


def test_committed_lost_acknowledgement_reuses_original_uuid(catalog, monkeypatch):
    source, _, _, connect = catalog
    twitter.adopt(source.source_id)
    token = uuid4()
    @contextmanager
    def lost():
        with connect() as db:
            yield db
            if db.execute('SELECT 1 FROM twitter_downloads').fetchone():
                raise OSError('Intentional committed reply loss')
    monkeypatch.setattr(jobs, 'get_user_db', lost)
    with pytest.raises(OSError, match='reply loss'):
        jobs.create('https://x.com/SavedAuthor/status/333', source.source_id, token)
    monkeypatch.setattr(jobs, 'get_user_db', connect)
    assert new_job(catalog, token)['id'] == str(token)
    assert job_count(catalog) == 1


def test_pending_restart_and_retry_receipts_do_not_start_remote_work(catalog):
    job = new_job(catalog)
    jobs.recover()
    assert jobs.get(job['id'])['status'] == 'interrupted'
    token = uuid4()
    retry = jobs.retry(job['id'], token)
    assert retry['status'] == 'queued' and retry['id'] == job['id']
    assert jobs.retry(job['id'], token) == retry
    jobs.cancel(job['id'])
    assert jobs.retry(job['id'], token)['status'] == 'cancelled'
    assert jobs.retry(job['id'], uuid4())['status'] == 'queued'
    assert not list(catalog[1].rglob('manifest.json'))


def test_explicit_retry_preserves_new_attempt_directories(catalog):
    job = new_job(catalog)
    jobs.update(job['id'], status='running')
    first = jobs.prepare(job)
    original = acquisition.directory(first) / 'source.json'
    original.write_bytes(b'preserved failed response')
    jobs.update(job['id'], status='failed')
    jobs.retry(job['id'], uuid4())
    jobs.update(job['id'], status='running')
    second = jobs.prepare(jobs.get(job['id']))
    assert first['path'] != second['path'] and second['attempt'] == 2
    assert original.read_bytes() == b'preserved failed response'


@pytest.mark.parametrize('change', ['hide', 'role', 'nested', 'missing'])
def test_destination_reconciles_fresh_ownership_without_output(catalog, change):
    source, media, _, connect = catalog
    job = new_job(catalog)
    with connect() as db:
        if change == 'hide':
            sources.update_source(db, source.source_id, visible=False)
        elif change == 'role':
            sources.update_source(db, source.source_id, role='files')
        elif change == 'nested':
            child = media / 'captures'; child.mkdir()
            sources.register_source(db, child, 'Nested owner')
        else:
            media.rmdir()
    jobs.update(job['id'], status='running')
    with pytest.raises((ValueError, serving.ServeDenied)):
        jobs.prepare(job)
    assert not list(media.rglob('manifest.json'))


def test_preferences_additive_schema_and_maintenance_exclusion(catalog):
    import maintenance
    source, _, _, connect = catalog
    twitter.adopt(source.source_id)
    with connect() as db:
        db.execute('CREATE TABLE marker(value TEXT)')
        db.execute("INSERT INTO marker VALUES('user original')")
        db.commit()
        twitter.publish(db)
        twitter.publish(db)
        assert db.execute('SELECT value FROM marker').fetchone()[0] == 'user original'
    assert jobs.save_preferences(source.source_id)['source_id'] == source.source_id
    with maintenance.module_transition():
        with pytest.raises(RuntimeError, match='transition'):
            new_job(catalog)
    assert job_count(catalog) == 0


def test_jobs_use_the_production_dictionary_row_factory(catalog, monkeypatch):
    source, _, _, connect = catalog
    @contextmanager
    def production_rows():
        with connect() as db:
            db.row_factory = database._row_factory
            yield db
    monkeypatch.setattr(jobs, 'get_user_db', production_rows)
    job = new_job(catalog)
    assert job_count(catalog) == 1
    assert jobs.get(job['id'])['status'] == 'queued'


@pytest.mark.parametrize('url', ['http://video.twimg.com/a.mp4', 'https://video.twimg.com.evil/a.mp4',
    'https://user:secret@video.twimg.com/a.mp4', 'https://video.twimg.com:123/a.mp4',
    'file:///video.mp4', 'https://127.0.0.1/video.mp4', 'https://video.twimg.com/a.mp4#fragment'])
def test_media_host_admission_precedes_any_request(url):
    with pytest.raises(ValueError):
        acquisition.remote_url(url, 'video.twimg.com')


def test_redirect_host_is_checked_before_following():
    from urllib.request import Request
    handler = acquisition.Redirects('video.twimg.com')
    request = Request('https://video.twimg.com/a.mp4')
    with pytest.raises(ValueError):
        handler.redirect_request(request, None, 302, 'Found', {}, 'https://outside.invalid/a.mp4')
    allowed = handler.redirect_request(request, None, 302, 'Found', {}, 'https://video.twimg.com/b.mp4')
    assert allowed.full_url == 'https://video.twimg.com/b.mp4'


@pytest.mark.parametrize('field', ['multiple', 'hls', 'avatar', 'video', 'author', 'text', 'posted'])
def test_unsupported_sources_preserve_response_and_refuse_record(field):
    source = source_response()
    if field == 'multiple': source['mediaDetails'].append(source['mediaDetails'][0])
    elif field == 'hls': source['mediaDetails'][0]['video_info']['variants'][0]['content_type'] = 'application/x-mpegURL'
    elif field == 'avatar': source['user']['profile_image_url_https'] = 'https://evil.invalid/avatar.jpg'
    elif field == 'video': source['mediaDetails'][0]['video_info']['variants'][0]['url'] = 'https://evil.invalid/video.mp4'
    elif field == 'author': source['user']['id_str'] = 123
    elif field == 'text': source['text'] = '\ud800'
    else: source['created_at'] = '2026-10-11T12:00:00'
    with pytest.raises(ValueError):
        acquisition.record({'post_id': '333', 'capture_id': 'fixture', 'url': 'https://x.com/SavedAuthor/status/333',
                            'captured_at': '2026-10-11T12:00:00Z'}, source)


def test_normalization_preserves_exact_text_and_optional_author_id():
    source = source_response()
    source['user'].pop('id_str')
    source.pop('created_at')
    spec = {'post_id': '333', 'capture_id': 'fixture', 'url': 'https://x.com/SavedAuthor/status/333',
            'captured_at': '2026-10-11T12:00:00Z'}
    tweet, _, _ = acquisition.record(spec, source)
    assert tweet['raw_text'] == RAW_TEXT and tweet['author']['id'] is None and tweet['posted_at'] is None


def test_real_helper_download_verify_publish_and_local_registration_retry(catalog, monkeypatch, tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from io import BytesIO
    from PIL import Image
    import imageio_ffmpeg
    generated = tmp_path / 'video.mp4'
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-v', 'error', '-f', 'lavfi', '-i', 'color=blue:s=48x48:r=4',
                    '-t', '0.5', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(generated)], check=True, timeout=30)
    avatar = BytesIO(); Image.new('RGB', (24, 24), 'blue').save(avatar, 'JPEG')
    bodies = {'/source.json': json.dumps(source_response(), ensure_ascii=False).encode('utf-8'),
              '/avatar.jpg': avatar.getvalue(), '/video.mp4': generated.read_bytes()}
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_HEAD(self): self.respond(False)
        def do_GET(self): self.respond(True)
        def respond(self, body):
            requests.append(self.path)
            from urllib.parse import urlsplit
            path = urlsplit(self.path).path
            value = bodies[path]
            self.send_response(200); self.send_header('Content-Length', str(len(value)))
            self.send_header('Content-Type', 'video/mp4' if path.endswith('.mp4') else 'application/octet-stream')
            self.end_headers()
            if body: self.wfile.write(value)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    original_launch = asyncio.create_subprocess_exec
    endpoint = f'http://127.0.0.1:{server.server_port}'
    script = 'import sys;sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[2]);from tests.browser_twitter_fixture import fixture_worker;raise SystemExit(fixture_worker(sys.argv[3]))'
    async def launch(*args, **kwargs):
        if '--twitter-worker' not in args:
            return await original_launch(*args, **kwargs)
        return await original_launch(sys.executable, '-c', script, str(ROOT), str(ROOT / 'backend'), endpoint, **kwargs)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    try:
        job = new_job(catalog)
        jobs.update(job['id'], status='running')
        original_scan = index.scan_source
        monkeypatch.setattr(index, 'scan_source', lambda *a, **kw: (_ for _ in ()).throw(OSError('Intentional index failure')))
        with pytest.raises(OSError, match='index failure'):
            asyncio.run(jobs.execute(job))
        item = jobs.get(job['id'])
        assert item['phase'] == 'registering'
        path = catalog[1] / item['path']; assert path.is_file()
        original = {p: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}
        manifest = json.loads(path.read_text(encoding='utf-8'))
        for name, facts in manifest['files'].items():
            assert hashlib.sha256((path.parent / name).read_bytes()).hexdigest() == facts['sha256']
        assert json.loads((path.parent / 'tweet.json').read_text(encoding='utf-8'))['raw_text'] == RAW_TEXT
        monkeypatch.setattr(index, 'scan_source', original_scan)
        jobs.update(job['id'], status='failed')
        jobs.retry(job['id'], uuid4())
        async def recover_registration():
            task = asyncio.create_task(jobs.run_queue())
            try:
                for _ in range(100):
                    if jobs.get(job['id'])['status'] == 'complete':
                        return
                    await asyncio.sleep(.05)
                raise AssertionError('Registration retry did not complete')
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        asyncio.run(recover_registration())
        assert jobs.get(job['id'])['status'] == 'complete'
        assert original == {p: p.read_bytes() for p in original}
        assert browse()['items'][0]['raw_text'] == RAW_TEXT
        assert requests.count('/source.json?id=333') == 1 and '/video.mp4' in requests
        repeated = new_job(catalog)
        jobs.update(repeated['id'], status='running')
        asyncio.run(jobs.execute(repeated))
        assert len(browse()['items']) == 2
        assert original == {p: p.read_bytes() for p in original}
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


@pytest.mark.parametrize('action', ['cancel', 'disable', 'source'])
def test_live_worker_stop_preserves_partial_files_and_reaps_child(catalog, monkeypatch, action):
    launched = []
    original_launch = asyncio.create_subprocess_exec
    script = ('import json,sys,time;from pathlib import Path;s=json.loads(sys.stdin.readline());'
              'p=Path(s["root"])/s["path"];(p.parent/"source.json").write_bytes(b"preserved partial");time.sleep(60)')
    async def launch(*args, **kwargs):
        if '--twitter-worker' not in args:
            return await original_launch(*args, **kwargs)
        process = await original_launch(sys.executable, '-c', script, **kwargs)
        launched.append(process)
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    job = new_job(catalog)
    async def check():
        runner = asyncio.create_task(jobs.run_queue())
        partial = None
        try:
            for _ in range(100):
                item = jobs.get(job['id'])
                if item['path']:
                    partial = catalog[1] / item['path'].replace('manifest.json', 'source.json')
                    if partial.is_file(): break
                await asyncio.sleep(.05)
            assert partial is not None and partial.is_file()
            if action == 'cancel':
                jobs.cancel(job['id'])
            elif action == 'source':
                with catalog[3]() as db:
                    sources.update_source(db, catalog[0].source_id, visible=False)
            else:
                runner.cancel()
            for _ in range(100):
                if launched[0].returncode is not None: break
                await asyncio.sleep(.05)
            assert launched[0].returncode is not None
            assert partial.read_bytes() == b'preserved partial'
            assert not partial.with_name('manifest.json').exists()
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
    asyncio.run(check())
    assert jobs.get(job['id'])['status'] == {'cancel':'cancelled','disable':'interrupted','source':'failed'}[action]

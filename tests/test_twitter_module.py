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

import pytest
from fastapi import HTTPException
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
import config
import database
from files_base import index, sources, serving
from modules import twitter
from modules.twitter import archive, router
from tests.browser_twitter_fixture import RAW_TEXT, write_capture


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
        source = sources.register_source(connection, media, 'Saved posts')
    monkeypatch.setattr(router, 'get_user_db', connect)
    monkeypatch.setattr(database, 'get_user_db', connect)
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

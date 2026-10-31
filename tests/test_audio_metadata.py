"""Real generated formats, preservation, derived cache and bounded TinyTag work."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import struct
import sys
import threading
import time
from unittest.mock import patch
import subprocess

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from files_base import index, serving, sources
from services import audio_metadata as audio
from tests.browser_ost_fixture import flac_comments, write_audio


@pytest.fixture(scope='module')
def formats(tmp_path_factory):
    import imageio_ffmpeg
    directory = tmp_path_factory.mktemp('generated-audio')
    result = {}
    for ext, codec in [('flac', 'flac'), ('mp3', 'libmp3lame'), ('m4a', 'aac'),
                       ('ogg', 'libvorbis'), ('wav', 'pcm_s16le')]:
        path = directory / ('generated.' + ext)
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.4', '-metadata', 'title=曲 / 한글',
            '-metadata', 'artist=Artist', '-metadata', 'album=Album', '-metadata', 'composer=Composer',
            '-metadata', 'genre=Ambient', '-metadata', 'track=4/12', '-metadata', 'disc=2/3',
            '-metadata', 'date=2026', '-c:a', codec, str(path)], check=True, timeout=30)
        result[ext] = path.read_bytes()
    path = directory / 'generated.flac'
    flac_comments(path, ['TITLE=  曲 / 한글  ', 'ARTIST=Artist one', 'ARTIST=歌手 二',
                         'ALBUM=Album', 'TRACKNUMBER=4/12', 'DISCNUMBER=2/3',
                         'CUSTOM_FIELD= extra value ', 'COMMENT=<script>inert()</script>'])
    result['multi'] = path.read_bytes()
    return result


@pytest.fixture
def catalog(tmp_path):
    media = tmp_path / 'media'; media.mkdir()
    with sqlite3.connect(tmp_path / 'user.sqlite') as connection:
        connection.row_factory = sqlite3.Row
        sources.ensure_sources_schema(connection)
        source = sources.register_source(connection, media, 'Existing OST name')
    return source, media, tmp_path / 'base/files.sqlite', tmp_path / 'private'


def read(catalog, relative):
    source, _, db, private = catalog
    return audio.read(db, source, relative, [private])


def cached(catalog, relative):
    source, _, db, private = catalog
    with index.open_index(db) as connection:
        return audio.cached(connection, source, relative, [private])


@pytest.mark.parametrize('ext', ['flac', 'mp3', 'm4a', 'ogg', 'wav', 'oga'])
def test_real_formats_tags_duration_and_unchanged_original(catalog, formats, ext):
    _, media, _, _ = catalog
    path = media / ('long filename 曲.' + ext)
    path.write_bytes(formats['ogg' if ext == 'oga' else ext])
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    assert cached(catalog, path.name)['status'] == 'pending'
    result = read(catalog, path.name)
    assert result['status'] == 'ready'
    assert result['tags']['title'] == ['曲 / 한글']
    assert result['tags']['artist'] == ['Artist']
    assert result['tags']['album'] == ['Album']
    assert result['display_title'] == '曲 / 한글'
    assert 0.25 < result['duration'] < 1 and result['samplerate'] > 0
    assert result['channels'] == 1 and result['mime_type'].startswith('audio/')
    assert cached(catalog, path.name) == result
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original


def test_multiple_values_extra_text_and_display_are_separate(catalog, formats):
    _, media, _, _ = catalog
    (media / 'filename.flac').write_bytes(formats['multi'])
    result = read(catalog, 'filename.flac')
    assert result['tags']['title'] == ['  曲 / 한글  ']
    assert result['tags']['artist'] == ['Artist one', '歌手 二']
    assert result['tags']['custom_field'] == [' extra value ']
    assert result['tags']['comment'] == ['<script>inert()</script>']
    assert result['tags']['track'] == ['4'] and result['tags']['track_total'] == ['12']
    assert result['tags']['disc'] == ['2'] and result['tags']['disc_total'] == ['3']
    assert result['display_title'] == '曲 / 한글'
    assert 'filename' not in result['tags'] and 'filesize' not in result['tags']


def test_missing_corrupt_unsupported_and_filename_fallback(catalog):
    _, media, _, _ = catalog
    write_audio(media / 'No tags 曲.wav')
    result = read(catalog, 'No tags 曲.wav')
    assert result['status'] == 'ready' and result['tags'] == {}
    assert result['display_title'] == 'No tags 曲' and result['duration'] == 4
    (media / 'broken.mp3').write_bytes(b'broken metadata')
    damaged = read(catalog, 'broken.mp3')
    assert damaged['tags'] == {} and damaged['duration'] is None
    assert damaged['display_title'] == 'broken'
    (media / 'truncated.flac').write_bytes(b'fLaC\x80\x00\x00\x22bad')
    assert read(catalog, 'truncated.flac') == audio.fallback('truncated.flac', 'invalid')
    (media / 'ignored.aac').write_bytes(b'unsupported preserved file')
    with patch.object(Path, 'open', side_effect=AssertionError('No unsupported audio read')):
        assert read(catalog, 'ignored.aac')['status'] == 'unsupported'
    with pytest.raises(serving.ServeDenied) as missing:
        read(catalog, 'missing.wav')
    assert missing.value.status_code == 404


def test_listing_and_warm_cache_never_open_audio(catalog):
    _, media, _, _ = catalog
    write_audio(media / 'track.wav')
    with patch.object(Path, 'open', side_effect=AssertionError('Listing must not parse audio')):
        assert cached(catalog, 'track.wav')['status'] == 'pending'
    result = read(catalog, 'track.wav')
    with patch.object(Path, 'open', side_effect=AssertionError('Warm cache must not parse audio')):
        assert read(catalog, 'track.wav') == result
        assert cached(catalog, 'track.wav') == result


def test_changed_file_same_integer_mtime_and_rescan_invalidate(catalog):
    source, media, db, _ = catalog
    path = media / 'track.wav'; write_audio(path, 4)
    with index.open_index(db) as connection:
        index.scan_source(connection, source.source_id, media)
    read(catalog, path.name)
    old = path.stat()
    write_audio(path, 3)
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 1))
    assert int(old.st_mtime) == int(path.stat().st_mtime)
    assert cached(catalog, path.name)['status'] == 'pending'
    assert read(catalog, path.name)['duration'] == 3
    with index.open_index(db) as connection:
        index.scan_source(connection, source.source_id, media)
        assert connection.execute('SELECT COUNT(*) FROM files_audio_metadata').fetchone()[0] == 0
    assert cached(catalog, path.name)['status'] == 'pending'
    read(catalog, path.name)
    with index.open_index(db) as connection:
        index.refresh_file(connection, source.source_id, media, path.name)
    assert cached(catalog, path.name)['status'] == 'pending'
    read(catalog, path.name)
    with index.open_index(db) as connection:
        index.drop_source(connection, source.source_id)
        assert connection.execute('SELECT COUNT(*) FROM files_audio_metadata').fetchone()[0] == 0
    assert path.is_file()


def test_cache_survives_reopen_and_repairs_bad_derived_payload(catalog):
    _, media, db, _ = catalog
    write_audio(media / 'track.wav'); read(catalog, 'track.wav')
    with index.open_index(db) as connection:
        connection.execute("UPDATE files_audio_metadata SET payload='not json'"); connection.commit()
    assert cached(catalog, 'track.wav')['status'] == 'pending'
    assert read(catalog, 'track.wav')['duration'] == 4
    with sqlite3.connect(db) as connection:
        assert json.loads(connection.execute('SELECT payload FROM files_audio_metadata').fetchone()[0])['duration'] == 4


@pytest.mark.parametrize('relative', ['../outside.wav', '/outside.wav', 'C:\\outside.wav'])
def test_invalid_paths_are_refused_before_audio_open(catalog, relative):
    with patch.object(Path, 'open', side_effect=AssertionError('Do not open escaped paths')):
        with pytest.raises(serving.ServeDenied) as error:
            read(catalog, relative)
    assert error.value.status_code == 400


def test_private_nested_and_symlink_guards_rechecked_on_warm_cache(catalog, tmp_path):
    source, media, db, private = catalog
    write_audio(media / 'nested/track.wav'); read(catalog, 'nested/track.wav')
    with pytest.raises(serving.ServeDenied) as nested:
        audio.read(db, source, 'nested/track.wav', [private, media / 'nested'])
    assert nested.value.status_code == 403
    outside = tmp_path / 'outside.wav'; write_audio(outside)
    (media / 'nested/track.wav').rename(media / 'preserved.wav')
    (media / 'nested/track.wav').symlink_to(outside)
    assert cached(catalog, 'nested/track.wav')['status'] == 'unavailable'
    with pytest.raises(serving.ServeDenied) as escaped:
        read(catalog, 'nested/track.wav')
    assert escaped.value.status_code == 403
    private.mkdir(); write_audio(private / 'secret.wav')
    (media / 'private').symlink_to(private, target_is_directory=True)
    with pytest.raises(serving.ServeDenied):
        read(catalog, 'private/secret.wav')


@pytest.mark.parametrize('budget', ['bytes', 'operations', 'time', 'result'])
def test_read_budgets_and_negative_cache_preserve_file(catalog, monkeypatch, budget):
    _, media, _, _ = catalog
    write_audio(media / 'track.wav')
    before = (media / 'track.wav').read_bytes()
    monkeypatch.setattr(audio, {'bytes': 'MAX_READ_BYTES', 'operations': 'MAX_OPERATIONS',
                              'time': 'READ_TIMEOUT', 'result': 'MAX_RESULT_BYTES'}[budget],
                        0 if budget != 'time' else -1)
    assert read(catalog, 'track.wav')['status'] == 'limited'
    assert (media / 'track.wav').read_bytes() == before
    # A caller timeout is deliberately not cached; late work cannot publish.
    if budget != 'time':
        assert cached(catalog, 'track.wav')['status'] in ('limited', 'pending')


def test_two_readers_no_queue_hard_wait_and_late_results_never_publish(catalog, monkeypatch):
    _, media, db, _ = catalog
    for name in ['one.wav', 'two.wav', 'three.wav']:
        write_audio(media / name)
    # Startup initializes the shared index before request concurrency begins.
    with index.open_index(db):
        pass
    entered = threading.Barrier(3); release = threading.Event()
    real = audio._extract
    monkeypatch.setattr(audio, '_readers', threading.BoundedSemaphore(2))
    monkeypatch.setattr(audio, 'READ_TIMEOUT', .1)
    def held(*args):
        entered.wait(timeout=3); release.wait(timeout=3)
        return real(*args)
    monkeypatch.setattr(audio, '_extract', held)
    try:
        with ThreadPoolExecutor(2) as callers:
            started = time.monotonic()
            first = callers.submit(read, catalog, 'one.wav')
            second = callers.submit(read, catalog, 'two.wav')
            entered.wait(timeout=3)
            assert read(catalog, 'three.wav')['status'] == 'busy'
            assert first.result(timeout=1)['status'] == second.result(timeout=1)['status'] == 'limited'
            assert time.monotonic() - started < 1
            release.set()
    finally:
        release.set()
    assert audio._readers.acquire(timeout=2)
    assert audio._readers.acquire(timeout=2)
    audio._readers.release(); audio._readers.release()
    with index.open_index(db) as connection:
        assert connection.execute('SELECT COUNT(*) FROM files_audio_metadata').fetchone()[0] == 0


def test_change_during_extraction_is_never_cached(catalog, monkeypatch):
    _, media, _, _ = catalog
    path = media / 'track.wav'; write_audio(path)
    real = audio._extract
    def changing(*args):
        result = real(*args); write_audio(path, 2)
        return result
    monkeypatch.setattr(audio, '_extract', changing)
    assert read(catalog, path.name)['status'] == 'unavailable'
    assert cached(catalog, path.name)['status'] == 'pending'


def test_cache_entry_cap_and_additive_existing_index(catalog, monkeypatch):
    source, media, db, _ = catalog
    monkeypatch.setattr(audio, 'MAX_CACHE_ENTRIES', 2)
    for name in ['one.wav', 'two.wav', 'three.wav']:
        write_audio(media / name); read(catalog, name)
    assert cached(catalog, 'one.wav')['status'] == 'pending'
    assert cached(catalog, 'two.wav')['status'] == 'ready'
    with index.open_index(db) as connection:
        index.scan_source(connection, source.source_id, media)
        connection.execute("UPDATE files_index SET content_hash='preserved hash'"); connection.commit()
        from files_base.schema import ensure_files_schema
        ensure_files_schema(connection)
        assert connection.execute('SELECT DISTINCT content_hash FROM files_index').fetchone()[0] == 'preserved hash'
        assert connection.execute('SELECT COUNT(*) FROM files_index').fetchone()[0] == 3


def test_reader_rejects_huge_request_before_allocating():
    bounded = audio._BudgetFile(BytesIO(b'preserved bytes'), time.monotonic() + 1)
    with pytest.raises(audio.BudgetExceeded):
        bounded.read(audio.MAX_READ_BYTES + 1)
    assert bounded.bytes == 0


def test_frozen_flag_uses_same_reader_without_external_helper(catalog, monkeypatch):
    _, media, _, _ = catalog
    write_audio(media / 'track.wav')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(media / 'nonexistent-Keivotos.exe'))
    with patch.object(subprocess, 'Popen', side_effect=AssertionError('No interpreter/helper process required')):
        assert read(catalog, 'track.wav')['duration'] == 4


def test_fresh_process_uses_persisted_cache_without_opening_audio(catalog):
    from dataclasses import asdict
    source, media, db, private = catalog
    write_audio(media / 'track.wav'); expected = read(catalog, 'track.wav')
    script = '''import json,sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from services import audio_metadata
source=SimpleNamespace(**json.loads(sys.argv[3]))
with patch.object(Path,'open',side_effect=AssertionError('Warm restart must not open audio')):
    result=audio_metadata.read(Path(sys.argv[1]),source,'track.wav',[Path(sys.argv[2])])
print(json.dumps(result,ensure_ascii=True))
'''
    environment = os.environ.copy()
    backend = str(Path(__file__).resolve().parents[1] / 'backend')
    environment['PYTHONPATH'] = backend + os.pathsep + environment.get('PYTHONPATH', '')
    child = subprocess.run([sys.executable, '-c', script, str(db), str(private), json.dumps(asdict(source))],
                           env=environment, capture_output=True, text=True, timeout=10)
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout) == expected

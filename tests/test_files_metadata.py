"""Passive preserved facts, strict source identity and original-byte safety."""
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from fastapi import HTTPException
from starlette.responses import Response

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
import config
from files_base import index, metadata, sources, serving
from module_registry import build_registry, ModuleRegistry
from modules import youtube
from modules.youtube import metadata as youtube_metadata
from routers import files
from tests.browser_twitter_fixture import write_capture, RAW_TEXT


@pytest.fixture(params=['row', 'dict'])
def catalog(tmp_path, monkeypatch, request):
    media = tmp_path / 'media'; media.mkdir()
    home = tmp_path / 'home'; home.mkdir()
    personal = home / 'user.sqlite'; db = home / 'files.sqlite'
    @contextmanager
    def connect():
        connection = sqlite3.connect(personal)
        connection.row_factory = sqlite3.Row if request.param == 'row' else lambda cursor, row: dict(zip([column[0] for column in cursor.description], row))
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()
    with connect() as connection:
        sources.ensure_sources_schema(connection)
        source = sources.register_source(connection, media)
        youtube._publish(connection)
    monkeypatch.setattr(files, 'get_user_db', connect)
    monkeypatch.setattr(config, 'FILES_DB_PATH', db)
    monkeypatch.setattr(config, 'SUITE_HOME', home)
    monkeypatch.setattr(config, 'MODULE_REGISTRY', build_registry(home, 'test'))
    return source, media, home, db, connect


def scan(catalog):
    source, media, _, db, _ = catalog
    with index.open_index(db) as connection:
        index.scan_source(connection, source.source_id, media)


def read(catalog, path):
    response = Response()
    value = files.get_module_metadata(response, catalog[0].source_id, path)
    assert response.headers['Cache-Control'] == 'no-store'
    return value['sections']


def fields(section):
    return {field['label']: field['value'] for field in section['fields']}


def download(catalog, **changes):
    source, media, _, _, connect = catalog
    values = dict(id='saved-job', source_id=source.source_id, root='D:\\old\\root',
        url='https://www.youtube.com/watch?v=abcdefghijk', video='720', audio='192',
        status='complete', path='saved/video.webm', thumbnail='saved/thumbnail.png',
        title='Captured title <script>bad()</script>', channel_name='Channel 한글',
        channel_id='UC'+'a'*22, captured_at='2026-10-20T12:00:00Z', media_ext='webm',
        duration_seconds=125, format_id='247+251', video_codec='vp9', audio_codec='opus')
    values.update(changes)
    for name in ('path', 'thumbnail'):
        if values[name]:
            target = media / values[name]; target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'preserved fixture')
    with connect() as connection:
        names = list(values)
        connection.execute('INSERT INTO youtube_downloads('+','.join(names)+') VALUES('+','.join('?' for _ in names)+')', list(values.values()))
    scan(catalog)
    return values


def test_download_facts_match_exact_saved_file_and_current_root_offline(catalog):
    job = download(catalog)
    before = {p: p.read_bytes() for p in catalog[1].rglob('*') if p.is_file()}
    user_bytes = catalog[2].joinpath('user.sqlite').read_bytes()
    section, = read(catalog, job['path'])
    facts = fields(section)
    assert section['owner'] == 'youtube' and facts['Source URL'] == job['url']
    assert facts['Saved file'] == str((catalog[1] / job['path']).resolve())
    assert 'D:' not in facts['Saved file'] and facts['Duration'] == '2:05'
    assert facts['Channel URL'] == youtube_metadata.channel_url(job['channel_id'])
    assert facts['Requested video'] == '720' and facts['Audio codec'] == 'opus'
    assert facts['Title'] == job['title']
    assert read(catalog, job['thumbnail'])[0]['fields'] == section['fields']
    assert before == {p: p.read_bytes() for p in before}
    assert catalog[2].joinpath('user.sqlite').read_bytes() == user_bytes


def test_legacy_download_missing_facts_and_no_filename_inference(catalog):
    job = download(catalog, channel_name='', channel_id='', captured_at='', media_ext='', duration_seconds=None)
    facts = fields(read(catalog, job['path'])[0])
    assert facts['Channel'] is facts['Channel ID'] is facts['Channel URL'] is facts['Downloaded'] is None
    other = catalog[1] / 'same named channel.webm'; other.write_bytes(b'ordinary file'); scan(catalog)
    assert read(catalog, other.name) == []
    with catalog[4]() as connection:
        connection.execute("UPDATE youtube_downloads SET status='failed'")
    assert read(catalog, job['path']) == []


def test_origin_provenance_and_properties_technical_facts_stay_separate(catalog):
    job = download(catalog)
    section, = read(catalog, job['path'])
    by_name = {field['label']: field for field in section['fields']}
    for name in ('Source URL', 'Title', 'Channel', 'Channel ID', 'Channel URL', 'Downloaded', 'Requested video', 'Requested audio'):
        assert by_name[name]['placement'] == 'origin'
    for name in ('File format', 'Format ID', 'Video codec', 'Audio codec', 'Duration', 'Saved file'):
        assert by_name[name]['placement'] == 'properties' and by_name[name]['href'] is None
    manifest = write_capture(catalog[1]); scan(catalog)
    twitter_section, = read(catalog, manifest)
    assert all(field['placement'] == 'origin' for field in twitter_section['fields'])


@pytest.mark.parametrize('mode', ['video', 'photo', 'text'])
def test_exact_twitter_archive_text_authors_and_local_relationships(catalog, mode):
    options = {} if mode == 'video' else {'photos': {'photo-1.jpg': b'preserved photo'}} if mode == 'photo' else {'photos': {}, 'current_format': True}
    path = write_capture(catalog[1], **options); scan(catalog)
    originals = {p: p.read_bytes() for p in (catalog[1] / path).parent.iterdir()}
    section, = read(catalog, path)
    facts = fields(section)
    assert section['owner'] == 'twitter' and facts['Post text'] == RAW_TEXT
    assert facts['Author'] == 'Saved author 이름' and facts['Handle'] == '@SavedAuthor'
    assert facts['Source URL'] == 'https://x.com/SavedAuthor/status/2000000'
    assert all('/source.json' not in asset['path'] for asset in section['related'])
    asset = 'video.mp4' if mode == 'video' else 'photo-1.jpg' if mode == 'photo' else 'tweet.json'
    assert read(catalog, str(Path(path).parent / asset).replace('\\', '/'))[0]['fields'] == section['fields']
    assert all(p.read_bytes() == data for p, data in originals.items())


def test_twitter_changed_manifest_and_missing_related_asset_do_not_invent_facts(catalog):
    path = write_capture(catalog[1]); scan(catalog)
    media = catalog[1] / Path(path).parent / 'video.mp4'; media.unlink()  # Generated fixture only.
    section, = read(catalog, path)
    assert any(not item['available'] and item['path'].endswith('video.mp4') for item in section['related'])
    manifest = catalog[1] / path
    value = json.loads(manifest.read_text()); value['post_id'] = '999'; manifest.write_text(json.dumps(value))
    section, = read(catalog, path)
    assert section['status'] == 'unavailable' and not section['fields']


@pytest.mark.parametrize('path,status', [('../escape', 400), ('/absolute', 400), ('D:/old/path', 400), ('missing', 404)])
def test_subject_guards(catalog, path, status):
    with pytest.raises(HTTPException) as error:
        read(catalog, path)
    assert error.value.status_code == status


def test_hidden_nested_private_sources_and_unindexed_files(catalog):
    job = download(catalog)
    source, media, home, _, connect = catalog
    with connect() as connection:
        child = sources.register_source(connection, media / 'saved')
        sources.update_source(connection, child.source_id, visible=False)
    with pytest.raises(HTTPException) as error:
        read(catalog, job['path'])
    assert error.value.status_code == 403
    with connect() as connection:
        sources.update_source(connection, source.source_id, visible=False)
    with pytest.raises(HTTPException) as error:
        read(catalog, '')
    assert error.value.status_code == 404
    with connect() as connection:
        sources.update_source(connection, source.source_id, visible=True)
    unindexed = media / 'fresh.txt'; unindexed.write_text('not indexed')
    with pytest.raises(HTTPException) as error:
        read(catalog, unindexed.name)
    assert error.value.status_code == 404
    with connect() as connection:
        private = sources.register_source(connection, home)
    with pytest.raises(HTTPException) as error:
        files.get_module_metadata(Response(), private.source_id, '')
    assert error.value.status_code == 403


def test_absent_contributions_and_failed_provider_preserve_plain_files(catalog, monkeypatch):
    job = download(catalog)
    registry = config.MODULE_REGISTRY
    monkeypatch.setattr(config, 'MODULE_REGISTRY', ModuleRegistry((registry.base,)))
    assert read(catalog, job['path']) == []
    with pytest.raises(HTTPException) as error:
        files.get_module_artwork(catalog[0].source_id, job['path'], 'youtube', 300, '')
    assert error.value.status_code == 404
    def broken(context):
        raise ValueError('generated failure')
    monkeypatch.setattr(config, 'MODULE_REGISTRY', registry.replacing(replace(registry.require('youtube'), file_metadata_provider=broken)))
    assert read(catalog, job['path'])[0]['status'] == 'unavailable'


def test_safe_links_and_late_source_visibility_rechecked(catalog, monkeypatch):
    job = download(catalog)
    registry = config.MODULE_REGISTRY
    def contribution(context):
        context.user_connection.execute('UPDATE files_sources SET visible=0 WHERE source_id=?', (context.source.source_id,))
        return {'fields': [{'label': 'Unsafe link', 'value': '<svg onload=alert(1)>', 'href': 'javascript:alert(1)'}]}
    assert metadata._link('javascript:alert(1)') is None
    assert metadata._link('https://user:pass@example.test') is None
    monkeypatch.setattr(config, 'MODULE_REGISTRY', registry.replacing(replace(registry.require('youtube'), file_metadata_provider=contribution)))
    with pytest.raises(HTTPException) as error:
        read(catalog, job['path'])
    assert error.value.status_code == 404


def test_passive_fresh_process_imports_no_jobs_workers_or_network(catalog):
    job = download(catalog)
    source, _, home, db, _ = catalog
    script = '''
import builtins, sqlite3, socket, sys
from pathlib import Path
sys.path.insert(0, 'backend')
from module_registry import build_registry
from files_base import metadata
original = builtins.__import__
def guarded(name, *args, **kwargs):
    assert not any(name == prefix or name.startswith(prefix+'.') for prefix in ('modules.youtube.jobs','modules.twitter.jobs','modules.youtube.worker','modules.twitter.worker')), name
    return original(name,*args,**kwargs)
builtins.__import__ = guarded
socket.create_connection = lambda *a,**k: (_ for _ in ()).throw(AssertionError('network'))
with sqlite3.connect(sys.argv[1]) as connection:
    connection.row_factory=sqlite3.Row
    ctx=metadata.context(connection,Path(sys.argv[2]),Path(sys.argv[3]),sys.argv[4],sys.argv[5])
    result=metadata.read(ctx,build_registry(Path(sys.argv[3]),'test'))
    assert result['sections'][0]['owner']=='youtube'
assert not any(name.endswith(('.jobs','.worker')) for name in sys.modules if name.startswith(('modules.youtube','modules.twitter')))
'''
    subprocess.run([sys.executable, '-c', script, str(home / 'user.sqlite'), str(db), str(home), source.source_id, job['path']], cwd=ROOT, check=True, timeout=15)


def test_duplicate_names_never_cross_source_identity(catalog):
    job = download(catalog)
    other_root = catalog[1].parent / 'other'; (other_root / 'saved').mkdir(parents=True)
    (other_root / job['path']).write_bytes(b'unrelated original')
    with catalog[4]() as connection:
        other = sources.register_source(connection, other_root)
    with index.open_index(catalog[3]) as connection:
        index.scan_source(connection, other.source_id, other_root)
    assert files.get_module_metadata(Response(), other.source_id, job['path'])['sections'] == []
    assert read(catalog, job['path'])[0]['owner'] == 'youtube'


def test_local_music_cover_reuses_thumbnail_and_never_writes_originals(catalog):
    from tests.browser_ost_fixture import write_audio
    from PIL import Image
    source, media, home, _, _ = catalog
    # WAV fixture helper is intentionally independent of metadata/network engines.
    write_audio(media / 'song.wav', 1)
    Image.new('RGB', (32, 48), '#7290ab').save(media / 'cover.png')
    scan(catalog)
    original = {p: p.read_bytes() for p in media.iterdir()}
    sections = read(catalog, 'song.wav')
    assert sections[0]['owner'] == 'ost' and sections[0]['artwork']
    response = files.get_module_artwork(source.source_id, 'song.wav', 'ost', 300, '')
    assert response.media_type == 'image/webp' and response.headers['Cache-Control'] == 'no-store'
    with Image.open(response.path) as cover:
        assert cover.size == (32, 48)
    assert original == {p: p.read_bytes() for p in original}
    assert not any(p.suffix == '.webp' for p in media.iterdir())


def test_symlink_metadata_and_artwork_cannot_escape(catalog):
    source, media, _, _, _ = catalog
    outside = media.parent / 'outside.wav'; outside.write_bytes(b'outside original')
    alias = media / 'alias.wav'
    alias.symlink_to(outside)
    scan(catalog)
    for operation in (lambda: read(catalog, alias.name),
                      lambda: files.get_module_artwork(source.source_id, alias.name, 'ost', 300, '')):
        with pytest.raises(HTTPException) as error:
            operation()
        assert error.value.status_code == 403
    assert outside.read_bytes() == b'outside original'


def test_new_youtube_bundle_requires_preserved_manifest(catalog):
    from modules.youtube import saved_item
    source, media, _, _, connect = catalog
    bundle = 'YouTube/Channel [UC' + 'a'*22 + ']/Saved [saved-job]'
    directory = media / bundle; directory.mkdir(parents=True)
    (directory / 'original.webm').write_bytes(b'original media')
    facts = youtube_metadata.capture({'id': 'abcdefghijk', 'channel_id': 'UC'+'a'*22, 'channel': 'Channel'},
                                     'https://www.youtube.com/watch?v=abcdefghijk', 'original.webm')
    base = {'id': 'saved-job', 'url': 'https://www.youtube.com/watch?v=abcdefghijk', 'video': 'best'}
    filename, _, digest = saved_item.prepare(directory, base, {'path': 'original.webm', 'title': 'Saved'}, facts)
    job = download(catalog, path=bundle+'/'+filename, thumbnail='', layout_version=1,
                   bundle_path=bundle, manifest_sha256=digest, **facts)
    assert read(catalog, job['path'])[0]['status'] == 'ready'
    assert fields(read(catalog, job['path'])[0])['Channel'] == 'Channel'
    manifest = directory / '.keivotos/item.json'; original = manifest.read_bytes()
    manifest.write_bytes(original + b' ')
    assert read(catalog, job['path'])[0]['status'] == 'unavailable'
    assert manifest.read_bytes() == original + b' '

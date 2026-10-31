"""Isolated OST discovery, registration, containment and preservation contracts."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import sys
import zipfile
from unittest.mock import patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import config
import database
from files_base import index, sources, serving
from modules import ost
from modules.ost import library, router


@pytest.fixture
def album_catalog(catalog):
    from tests.browser_ost_fixture import seed_music_albums
    from starlette.responses import Response
    original, connect, media, db = catalog
    album_root = media.parent / 'album music'
    seed_music_albums(album_root)
    with connect() as connection:
        source = sources.register_source(connection, album_root, 'Album library', role='ost')
    with index.open_index(db) as connection:
        index.scan_source(connection, source.source_id, album_root)
    before = {path.relative_to(album_root).as_posix(): path.read_bytes() for path in album_root.rglob('*') if path.is_file()}
    for relative in before:
        if Path(relative).suffix.lstrip('.') in library.AUDIO_EXTENSIONS:
            router.metadata(Response(), source.source_id, relative)
    yield source, connect, album_root, db
    assert before == {path.relative_to(album_root).as_posix(): path.read_bytes() for path in album_root.rglob('*') if path.is_file()}


def test_album_tags_discs_fallback_copies_and_order(album_catalog):
    source, _, _, _ = album_catalog
    result = router.albums(0, 120)
    albums = [album for album in result['items'] if album['source_id'] == source.source_id]
    assert len(albums) == 13
    assert len([album for album in albums if album['name'] == 'Blue']) == 2
    blue = next(album for album in albums if album['name'] == 'Blue' and album['anchor'].startswith('Blue/'))
    assert blue['total'] == 3 and not blue['pending'] and blue['artists'] == ['Album artist']
    detail = router.album_tracks(source.source_id, blue['anchor'], 0, 120)
    assert [item['music']['title'] for item in detail['items']] == ['Opening song', 'A later song', 'Finale 曲']
    assert [item['disc_label'] for item in detail['items']] == ['Disc 1', 'Disc 1', 'Disc 2']
    assert [item['music']['track'] for item in detail['items']] == [1, 2, 1]
    assert all(item['music']['duration'] == 20 for item in detail['items'])
    assert blue['covers'][1]['path'] == 'Blue'
    red = next(album for album in albums if album['name'] == 'Red')
    assert red['total'] == 2
    assert [item['disc_label'] for item in router.album_tracks(source.source_id, red['anchor'], 0, 120)['items']] == ['Part A', 'Part B']
    untagged = next(album for album in albums if album['name'] == 'Untagged')
    assert untagged['total'] == 3
    assert [item['name'] for item in router.album_tracks(source.source_id, untagged['anchor'], 0, 120)['items']] == ['Track 2.wav', 'Track 10.wav', '曲.wav']
    assert {album['name'] for album in albums}.issuperset({'Part A', 'Part B', 'One', 'Other', 'Unknown'})


def test_album_source_and_location_identity_pagination_and_queries(album_catalog):
    source, connect, media, db = album_catalog
    from starlette.responses import Response
    mirror = media.parent / 'mirror'; (mirror / 'Blue').mkdir(parents=True)
    (mirror / 'Blue/copy.flac').write_bytes((media / 'Blue/Disc 1/01 - First.flac').read_bytes())
    with connect() as connection:
        sibling = sources.register_source(connection, mirror, 'Same name', role='ost')
    with index.open_index(db) as connection:
        index.scan_source(connection, sibling.source_id, mirror)
    router.metadata(Response(), sibling.source_id, 'Blue/copy.flac')
    blue = router.albums(0, 120, 'bLuE')
    assert len(blue['items']) == 3 and {item['source_id'] for item in blue['items']} == {source.source_id, sibling.source_id}
    pages = [router.albums(offset, 2) for offset in range(0, router.albums(0, 120)['total'], 2)]
    assert [item['id'] for page in pages for item in page['items']] == [item['id'] for item in router.albums(0, 120)['items']]
    assert router.albums(999, 2)['items'] == []
    assert router.tracks(0, 120, '歌手 二')['total'] == 1
    assert router.tracks(0, 120, '100%_')['total'] == 1
    assert router.albums(0, 120, '100%_')['total'] == 1
    assert router.tracks(0, 120, 'Untagged')['total'] == 3
    assert router.albums(0, 120, 'Finale')['items'][0]['total'] == 3
    detail = router.album_tracks(source.source_id, 'Blue/Disc 1/01 - First.flac', 1, 1)
    assert detail['total'] == 3 and detail['items'][0]['music']['title'] == 'A later song'
    assert detail['items'][0]['source_id'] == source.source_id


def test_album_catalog_survives_raw_cache_eviction_and_unchanged_rescan(album_catalog, monkeypatch):
    from services import audio_metadata
    from starlette.responses import Response
    source, _, media, db = album_catalog
    monkeypatch.setattr(audio_metadata, 'MAX_CACHE_ENTRIES', 1)
    with index.open_index(db) as connection:
        connection.execute('DELETE FROM files_audio_metadata WHERE source_id=? AND relative_path=?', (source.source_id, 'Root track.flac'))
        connection.commit()
    router.metadata(Response(), source.source_id, 'Root track.flac')
    with index.open_index(db) as connection:
        assert connection.execute('SELECT COUNT(*) FROM files_audio_metadata').fetchone()[0] == 1
    assert router.tracks(0, 120, 'Opening song')['total'] == 1
    with patch.object(Path, 'open', side_effect=AssertionError('Catalog reads must not parse audio')):
        assert router.albums(0, 120, 'Finale')['items'][0]['total'] == 3
    with index.open_index(db) as connection:
        index.scan_source(connection, source.source_id, media)
    assert router.tracks(0, 120, 'Opening song')['total'] == 1
    assert all(item['music']['status'] == 'ready' for item in router.tracks(0, 120)['items'] if item['source_id'] == source.source_id)
    router.metadata(Response(), source.source_id, 'Blue/Disc 1/01 - First.flac')
    assert router.tracks(0, 120, 'Opening song')['total'] == 1


def test_metadata_record_synchronizes_only_its_selected_identity(album_catalog, monkeypatch):
    from modules.ost import albums
    from starlette.responses import Response
    source, _, _, db = album_catalog
    with index.open_index(db) as connection:
        before = {row['path']: tuple(row) for row in connection.execute('SELECT * FROM ost_catalog ORDER BY path')}
    calls = []
    original = albums.presentation

    def observed(name, metadata=None):
        calls.append(name)
        return original(name, metadata)

    monkeypatch.setattr(albums, 'presentation', observed)
    assert router.metadata(Response(), source.source_id, 'Root track.flac')['status'] == 'ready'
    assert calls and set(calls) == {'Root track.flac'}
    with index.open_index(db) as connection:
        after = {row['path']: tuple(row) for row in connection.execute('SELECT * FROM ost_catalog ORDER BY path')}
    assert {key: value for key, value in before.items() if key != 'Root track.flac'} == {
        key: value for key, value in after.items() if key != 'Root track.flac'}


def test_first_metadata_read_creates_only_one_catalog_row(catalog):
    from tests.browser_ost_fixture import write_audio
    from starlette.responses import Response
    source, _, media, db = catalog
    write_audio(media / 'Fresh.wav')
    ost.adopt(source.source_id)
    with index.open_index(db) as connection:
        index.scan_source(connection, source.source_id, media)
    assert router.metadata(Response(), source.source_id, 'Fresh.wav')['duration'] == 4
    with index.open_index(db) as connection:
        rows = connection.execute('SELECT path FROM ost_catalog').fetchall()
    assert [row['path'] for row in rows] == ['Fresh.wav']
    assert router.tracks(0, 120)['total'] > 1


def test_album_nested_hidden_private_guards_and_prepare(album_catalog):
    source, connect, media, db = album_catalog
    prepared = router.album_tracks(source.source_id, 'Red/Part A/one.flac', 0, 120, True)
    assert {item['path'] for item in prepared['items']} == {'Red/Part A/one.flac', 'Red/Part B/two.flac'}
    with connect() as connection:
        nested = sources.register_source(connection, media / 'Blue/Disc 2', 'Private child', role='files')
        sources.update_source(connection, nested.source_id, visible=False)
    blue = next(item for item in router.albums(0, 120)['items'] if item['source_id'] == source.source_id and item['anchor'].startswith('Blue/'))
    assert blue['total'] == 2
    with pytest.raises(HTTPException) as error:
        router.album_tracks(source.source_id, 'Blue/Disc 2/01 - Finale.flac', 0, 120)
    assert error.value.status_code == 403
    for path in ['../outside.flac', '/outside.flac', 'C:\\outside.flac']:
        with pytest.raises(HTTPException) as error:
            router.album_tracks(source.source_id, path, 0, 120)
        assert error.value.status_code == 400
    with connect() as connection:
        sources.update_source(connection, source.source_id, visible=False)
    assert not any(item['source_id'] == source.source_id for item in router.albums(0, 120)['items'])
    with pytest.raises(HTTPException) as error:
        router.album_tracks(source.source_id, 'Root track.flac', 0, 120)
    assert error.value.status_code == 404


@pytest.mark.parametrize('artists,expected', [([[], []], 1), ([['A'], ['B']], 2), ([['A', 'B'], ['b', 'a']], 1)])
def test_album_identity_uses_album_artists_not_per_track_artists(artists, expected):
    from modules.ost import albums
    rows = []
    for i in range(2):
        facts = albums.presentation(f'{i}.wav')
        facts.update(album='Same album', album_artists=artists[i], artists=[f'Track artist {i}'], status='ready')
        rows.append({'source_id': 'source', 'parent': 'Album', 'path': f'Album/{i}.wav', 'name': f'{i}.wav', 'music': facts})
    grouped = albums.group(rows, {'source': 'Library'})
    assert len(grouped) == expected
    assert sum(len(album['items']) for album in grouped) == 2


def test_invalid_derived_album_fields_rebuild_without_changing_tracks(album_catalog):
    import json
    from modules.ost import albums
    source, _, _, db = album_catalog
    invalid = albums.presentation('original.flac')
    invalid.update(title=None, artists='invalid list', duration='not a duration')
    with index.open_index(db) as connection:
        connection.execute('UPDATE ost_catalog SET facts=? WHERE source_id=?', (json.dumps(invalid), source.source_id))
        connection.commit()
    listing = router.tracks(0, 120, 'Opening song')
    assert listing['total'] == 1 and listing['items'][0]['music']['title'] == 'Opening song'
    blue = [item for item in router.albums(0, 120)['items'] if item['name'] == 'Blue']
    assert len(blue) == 2 and sum(item['total'] for item in blue) == 4


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    home = tmp_path / 'metadata'
    home.mkdir()
    media = tmp_path / 'music'
    media.mkdir()
    for folder in ['Album 2', 'Album 10']:
        (media / folder).mkdir()
        (media / folder / '曲 1.wav').write_bytes(b'original-audio')
    for name in ['Track 10.MP3', 'Track 2.mp3', '100%.flac', 'sample.wav',
                 'sample.ogg', 'sample.oga', 'sample.m4a', 'ignored.aac',
                 'ignored.mp4', 'cover.jpg', 'notes.md']:
        (media / name).write_bytes(b'original-audio')
    user = home / 'user.sqlite'
    db = home / 'base/files.sqlite'
    with sqlite3.connect(user) as connection:
        connection.row_factory = sqlite3.Row
        sources.ensure_sources_schema(connection)
        source = sources.register_source(connection, media, 'Soundtracks')
    with index.open_index(db) as connection:
        index.scan_source(connection, source.source_id, media)

    @contextmanager
    def connect():
        connection = sqlite3.connect(user)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()
    monkeypatch.setattr(router, 'get_user_db', connect)
    monkeypatch.setattr(database, 'get_user_db', connect)
    monkeypatch.setattr(config, 'USER_DB_PATH', user)
    monkeypatch.setattr(config, 'FILES_DB_PATH', db)
    monkeypatch.setattr(config, 'SUITE_HOME', home)
    return source, connect, media, db


def browse(source, parent='', offset=0, limit=120):
    return router.library(source.source_id, parent, offset, limit)


def test_flat_tracks_all_visible_ost_roots_and_full_identity(catalog):
    source, connect, media, db = catalog
    before = {str(path): path.read_bytes() for path in media.rglob('*') if path.is_file()}
    assert router.tracks(0, 120) == {'items': [], 'total': 0}
    ost.adopt(source.source_id)
    with connect() as connection:
        sibling_path = media.parent / 'second music'
        (sibling_path / 'Album 2').mkdir(parents=True)
        (sibling_path / 'Album 2/曲 1.wav').write_bytes(b'different preserved audio')
        sibling = sources.register_source(connection, sibling_path, 'Soundtracks')
    ost.adopt(sibling.source_id)
    with index.open_index(db) as connection:
        index.scan_source(connection, sibling.source_id, sibling_path)
    result = router.tracks(0, 240)
    assert result['total'] == 10
    assert all(not item['is_dir'] and item['ext'] in library.AUDIO_EXTENSIONS for item in result['items'])
    twins = [item for item in result['items'] if item['path'] == 'Album 2/曲 1.wav']
    assert {item['source_id'] for item in twins} == {source.source_id, sibling.source_id}
    assert all(item['source_name'] == 'Soundtracks' and item['parent'] == 'Album 2' for item in twins)
    expected = sorted(result['items'], key=lambda item: (library._natural(item['source_name']), library._natural(item['path']), item['source_id']))
    assert result['items'] == expected
    pages = [router.tracks(offset, 3) for offset in range(0, result['total'], 3)]
    assert [item for page in pages for item in page['items']] == result['items']
    assert all(page['total'] == result['total'] for page in pages)
    assert router.tracks(999, 3)['items'] == []
    assert before == {str(path): path.read_bytes() for path in media.rglob('*') if path.is_file()}
    with connect() as connection:
        sources.update_source(connection, sibling.source_id, visible=False)
    assert {item['source_id'] for item in router.tracks(0, 240)['items']} == {source.source_id}


def test_flat_tracks_nested_ownership_missing_files_and_no_audio_reads(catalog):
    source, connect, media, db = catalog
    ost.adopt(source.source_id)
    with connect() as connection:
        child = sources.register_source(connection, media / 'Album 2', 'Nested')
    # Parent index still contains the child; registration itself establishes ownership.
    parent = router.tracks(0, 240)
    assert not any(item['path'].startswith('Album 2/') for item in parent['items'])
    ost.adopt(child.source_id)
    with index.open_index(db) as connection:
        index.scan_source(connection, child.source_id, media / 'Album 2')
    (media / 'sample.wav').rename(media / 'sample.wav.preserved')
    original_read = Path.open
    def guarded_open(path, *args, **kwargs):
        assert path.suffix.lower().lstrip('.') not in library.AUDIO_EXTENSIONS
        return original_read(path, *args, **kwargs)
    with patch.object(Path, 'open', guarded_open):
        result = router.tracks(0, 240)
    assert sum(item['source_id'] == child.source_id for item in result['items']) == 1
    assert next(item for item in result['items'] if item['path'] == 'sample.wav')['available'] is False
    with connect() as connection:
        sources.update_source(connection, child.source_id, visible=False)
    assert not any(item['source_id'] == child.source_id or item['path'].startswith('Album 2/') for item in router.tracks(0, 240)['items'])


def test_flat_tracks_rejects_stale_private_and_escape_index_entries(catalog):
    source, connect, media, db = catalog
    ost.adopt(source.source_id)
    with index.open_index(db) as connection:
        connection.execute("UPDATE files_index SET relative_path='../outside.wav' WHERE source_id=? AND relative_path='sample.wav'", (source.source_id,))
        connection.execute("UPDATE files_index SET relative_path=? WHERE source_id=? AND relative_path='sample.ogg'", (str(config.SUITE_HOME / 'private.ogg'), source.source_id))
        (config.SUITE_HOME / 'private.ogg').write_bytes(b'precious private bytes')
    result = router.tracks(0, 240)
    assert all(not item['available'] for item in result['items'] if item['path'] in ('../outside.wav', str(config.SUITE_HOME / 'private.ogg')))


def test_descriptor_is_experimental_and_uses_existing_index_only(tmp_path):
    descriptor = ost.descriptor(tmp_path, 'test')
    assert descriptor.name == 'Music' and descriptor.description == 'Local music folders.'
    assert descriptor.slug == 'ost' and descriptor.experimental and descriptor.disableable
    assert descriptor.home == tmp_path / 'modules/ost'
    assert descriptor.api_prefix == '/api/ost' and descriptor.log_prefix == 'ost'
    assert descriptor.user_agent == 'Keivotos/test (OST)'
    assert descriptor.database == tmp_path / 'base/files.sqlite'
    assert descriptor.credentials is None and descriptor.config_defaults == {}
    assert descriptor.user_schema_provider is None and descriptor.index_initializer is None
    assert descriptor.backup_components() == ()
    tasks = descriptor.background_tasks()
    assert [name for name, _ in tasks] == ['ost-metadata']
    for _, coroutine in tasks:
        coroutine.close()
    assert descriptor.startup_hook is not None and descriptor.index_rescan_hook is not None
    assert descriptor.routers()[0] is router.router
    assert set(library.AUDIO_EXTENSIONS) == set(serving._INLINE_AUDIO)


def test_music_and_files_share_guarded_metadata_without_listing_decodes(catalog, monkeypatch):
    import asyncio
    import json
    from types import SimpleNamespace
    from urllib.parse import urlencode
    from fastapi import FastAPI
    from routers import files as files_router
    from services import audio_metadata
    from tests.browser_ost_fixture import write_audio
    source, connect, media, db = catalog
    monkeypatch.setattr(files_router, 'get_user_db', connect)
    write_audio(media / 'sample.wav')
    ost.adopt(source.source_id)
    original_user = config.USER_DB_PATH.read_bytes()
    app = FastAPI(); app.include_router(router.router); app.include_router(files_router.router)

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def get(self, route, params):
            messages = []
            async def receive(): return {'type': 'http.request', 'body': b'', 'more_body': False}
            async def send(message): messages.append(message)
            await app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                       'method': 'GET', 'scheme': 'http', 'path': route, 'raw_path': route.encode(),
                       'root_path': '', 'query_string': urlencode(params).encode(), 'headers': [],
                       'client': ('127.0.0.1', 1), 'server': ('127.0.0.1', 80)}, receive, send)
            start = next(message for message in messages if message['type'] == 'http.response.start')
            data = json.loads(b''.join(message.get('body', b'') for message in messages if message['type'] == 'http.response.body'))
            return SimpleNamespace(status_code=start['status'], json=lambda: data,
                headers={key.decode(): value.decode() for key, value in start['headers']})

    async def exercise():
        async with Client() as client:
            parameters = {'source_id': source.source_id, 'path': 'sample.wav'}
            with patch.object(audio_metadata, '_extract', side_effect=AssertionError('Listing must not decode')):
                assert next(item for item in browse(source)['items'] if item['name'] == 'sample.wav')['audio_metadata']['status'] == 'pending'
                assert next(item for item in router.tracks(0, 240)['items'] if item['name'] == 'sample.wav')['audio_metadata']['status'] == 'pending'
            response = await client.get('/api/ost/metadata', params=parameters)
            assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
            assert response.json()['duration'] == 4
            with patch.object(audio_metadata, '_extract', side_effect=AssertionError('Shared cache must avoid decoding')):
                files = await client.get('/api/files/audio-metadata', params=parameters)
                assert files.status_code == 200 and files.json() == response.json()
                assert files.headers['cache-control'] == 'no-store'
                assert next(item for item in router.tracks(0, 240)['items'] if item['name'] == 'sample.wav')['audio_metadata'] == files.json()
            for route in ['/api/files/audio-metadata', '/api/ost/metadata']:
                assert (await client.get(route, params={**parameters, 'path': '../outside.wav'})).status_code == 400
                assert (await client.get(route, params={**parameters, 'path': 'Album 2'})).status_code == 404
            assert config.USER_DB_PATH.read_bytes() == original_user
            with connect() as connection:
                child = sources.register_source(connection, media / 'Album 2', 'Nested', role='files')
            for route in ['/api/files/audio-metadata', '/api/ost/metadata']:
                assert (await client.get(route, params={**parameters, 'path': 'Album 2/曲 1.wav'})).status_code == 403
                assert (await client.get(route, params={'source_id': child.source_id, 'path': '曲 1.wav'})).status_code == (200 if route.startswith('/api/files/') else 404)
            with connect() as connection:
                sources.update_source(connection, source.source_id, visible=False)
            for route in ['/api/files/audio-metadata', '/api/ost/metadata']:
                assert (await client.get(route, params=parameters)).status_code == 404
    asyncio.run(exercise())
    # Only the explicit fixture registrations above mutate the user registry.
    with connect() as connection:
        assert sources.get_source(connection, source.source_id).display_name == 'Soundtracks'
    assert original_user and (media / 'sample.wav').is_file()


def test_role_visible_sources_natural_order_and_pagination(catalog):
    source, connect, media, _ = catalog
    assert router.folders() == []
    with pytest.raises(HTTPException) as error:
        browse(source)
    assert error.value.status_code == 404
    assert error.value.detail == 'Music folder is unavailable'
    ost.adopt(source.source_id)
    roots = router.folders()
    assert len(roots) == 1
    assert {key: roots[0][key] for key in ('source_id', 'name', 'available')} == {
        'source_id': source.source_id, 'name': 'Soundtracks', 'available': True}
    assert roots[0]['path'] == str(media)
    assert len(roots[0]['cover_version']) == 24
    result = browse(source)
    assert result['total'] == 9
    assert [item['name'] for item in result['items'][:2]] == ['Album 2', 'Album 10']
    tracks = [item['name'] for item in result['items'] if not item['is_dir']]
    assert tracks.index('Track 2.mp3') < tracks.index('Track 10.MP3')
    assert not any(item['name'].startswith('ignored') for item in result['items'])
    assert browse(source, '', 2, 3)['items'] == result['items'][2:5]
    assert browse(source, '', 1000, 3)['items'] == []
    assert browse(source, 'Album 2')['items'][0]['name'] == '曲 1.wav'
    assert browse(source, '.')['parent'] == ''
    with connect() as connection:
        sources.update_source(connection, source.source_id, visible=False)
    assert router.folders() == []
    with pytest.raises(HTTPException) as error:
        browse(source)
    assert error.value.status_code == 404
    assert error.value.detail == 'Music folder is unavailable'
    assert (media / 'Track 2.mp3').read_bytes() == b'original-audio'


def test_discovery_opens_no_audio_bytes(catalog):
    source, _, _, _ = catalog
    ost.adopt(source.source_id)
    with patch.object(Path, 'read_bytes', side_effect=AssertionError('Do not read media')):
        assert browse(source)['total'] == 9


def test_nested_registered_roots_are_refused_even_with_stale_parent_index(catalog):
    source, connect, media, _ = catalog
    ost.adopt(source.source_id)
    with connect() as connection:
        nested = sources.register_source(connection, media / 'Album 2', 'Separate', role='files')
        sources.update_source(connection, nested.source_id, visible=False)
    assert 'Album 2' not in [item['name'] for item in browse(source)['items']]
    with pytest.raises(HTTPException) as error:
        browse(source, 'Album 2')
    assert error.value.status_code == 403
    with connect() as connection:
        sources.update_source(connection, nested.source_id, role='ost', visible=True)
    assert len(router.folders()) == 2
    with pytest.raises(HTTPException) as error:
        browse(source, 'Album 2')
    assert error.value.status_code == 403
    assert (media / 'Album 2/曲 1.wav').read_bytes() == b'original-audio'


@pytest.mark.parametrize('parent', ['../outside', '/outside', 'C:\\outside', 'Album 2/../Album 10'])
def test_relative_parent_guards(catalog, parent):
    source, _, _, _ = catalog
    ost.adopt(source.source_id)
    with pytest.raises(HTTPException) as error:
        browse(source, parent)
    assert error.value.status_code == 400


def test_symlinks_missing_files_and_private_data_remain_guarded(catalog, tmp_path):
    source, _, media, db = catalog
    ost.adopt(source.source_id)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'hidden.mp3').write_bytes(b'outside')
    (media / 'Escape').symlink_to(outside, target_is_directory=True)
    (media / 'Private').symlink_to(config.SUITE_HOME, target_is_directory=True)
    (media / 'alias.mp3').symlink_to(outside / 'hidden.mp3')
    with index.open_index(db) as connection:
        index.scan_source(connection, source.source_id, media)
    for parent in ['Escape', 'Private']:
        with pytest.raises(HTTPException) as error:
            browse(source, parent)
        assert error.value.status_code == 403
    assert not next(item for item in browse(source)['items'] if item['name'] == 'alias.mp3')['available']
    (media / 'Track 2.mp3').rename(media / 'preserved-moved.mp3')
    assert not next(item for item in browse(source)['items'] if item['name'] == 'Track 2.mp3')['available']
    media.rename(media.with_name('preserved-music'))
    assert router.folders()[0]['available'] is False
    with pytest.raises(HTTPException) as error:
        browse(source)
    assert error.value.status_code == 404


def test_release_and_forget_preserve_every_original(catalog):
    source, connect, media, _ = catalog
    before = {p.relative_to(media).as_posix(): p.read_bytes() for p in media.rglob('*') if p.is_file()}
    ost.adopt(source.source_id)
    ost.release(source.source_id)
    with connect() as connection:
        assert sources.get_source(connection, source.source_id).role == 'files'
    ost.adopt(source.source_id)
    ost.release(source.source_id, True)
    with connect() as connection:
        assert sources.get_source(connection, source.source_id) is None
    assert before == {p.relative_to(media).as_posix(): p.read_bytes() for p in media.rglob('*') if p.is_file()}


def test_storage_counts_shared_index_once_and_backup_contains_only_registry(catalog, tmp_path, monkeypatch):
    from services import storage_usage
    import backup_bundle
    source, _, media, _ = catalog
    ost.adopt(source.source_id)
    usage = storage_usage.library_files_usage()
    category = next(row for row in usage['categories'] if row['id'] == 'ost')
    assert category['name'] == 'Music'
    assert category['bytes'] == sum(p.stat().st_size for p in media.rglob('*') if p.is_file())
    assert usage['total_bytes'] == category['bytes']
    monkeypatch.setattr(backup_bundle, 'USER_DB_PATH', config.USER_DB_PATH)
    monkeypatch.setattr(config,'JOBS_DB_PATH',tmp_path/'music-test-state/jobs.sqlite')
    components = dict(backup_bundle.COMPONENTS)
    components['user_database'] = ('databases/user.sqlite', config.USER_DB_PATH)
    monkeypatch.setattr(backup_bundle, 'COMPONENTS', components)
    selection = {key: key == 'user_database' for key in components}
    monkeypatch.setattr(backup_bundle, 'get_backup_config', lambda: {'destination': str(tmp_path / 'backups'), 'components': selection})
    result = backup_bundle.create_backup_bundle(selection)
    with zipfile.ZipFile(result['path']) as archive:
        assert 'databases/user.sqlite' in archive.namelist()
        assert not any(name.endswith(('.mp3', '.wav', '.flac', 'ost.sqlite')) for name in archive.namelist())
        snapshot = tmp_path / 'backup-user.sqlite'
        snapshot.write_bytes(archive.read('databases/user.sqlite'))
    with sqlite3.connect(snapshot) as connection:
        assert connection.execute('SELECT role FROM files_sources WHERE source_id=?', (source.source_id,)).fetchone()[0] == 'ost'


@pytest.fixture
def art_cache(catalog, monkeypatch):
    import thumbnails
    monkeypatch.setattr(thumbnails, 'THUMB_DIR', config.SUITE_HOME / 'base/thumbnails')
    monkeypatch.setattr(thumbnails, '_schedule_thumbnail_prune', lambda: None)
    monkeypatch.setattr(thumbnails, '_file_md5', lambda _: (_ for _ in ()).throw(
        AssertionError('OST must not hash the entire audio for a thumbnail')))
    return catalog


def rescan(source, media, db):
    with index.open_index(db) as connection:
        index.scan_source(connection, source.source_id, media)


def cover_pixel(source, path=''):
    from PIL import Image
    response = router.cover(source.source_id, path, 300, None)
    assert response.media_type == 'image/webp'
    assert response.headers['cache-control'] == 'public, max-age=31536000, immutable'
    with Image.open(response.path) as image:
        return image.convert('RGB').getpixel((0, 0))


def test_named_cover_order_case_insensitivity_and_corrupt_fallthrough(art_cache):
    from PIL import Image
    from tests.browser_ost_fixture import write_audio
    source, _, media, db = art_cache
    ost.adopt(source.source_id)
    write_audio(media / 'sample.wav')
    (media / 'cover.jpg').write_bytes(b'generated invalid picture')
    Image.new('RGB', (40, 40), 'blue').save(media / 'COVER.PNG')
    Image.new('RGB', (40, 40), 'green').save(media / 'folder.jpg')
    Image.new('RGB', (40, 40), 'red').save(media / 'front.png')
    rescan(source, media, db)
    color = cover_pixel(source, 'sample.wav')
    assert color[2] > 200 and color[0] < 20
    assert cover_pixel(source) == color
    # All candidates at a higher priority now fail, so the next name wins.
    (media / 'COVER.PNG').write_bytes(b'generated corrupt replacement')
    rescan(source, media, db)
    assert cover_pixel(source, 'sample.wav')[1] > 100


def test_embedded_art_wins_and_cached_repeat_skips_decoding(art_cache, monkeypatch):
    from PIL import Image
    import thumbnails
    from tests.browser_ost_fixture import embedded_audio
    source, _, media, db = art_cache
    ost.adopt(source.source_id)
    picture = media / 'embedded.png'
    Image.new('RGB', (40, 40), 'orange').save(picture)
    embedded_audio(media / 'embedded.flac', picture)
    Image.new('RGB', (40, 40), 'blue').save(media / 'cover.jpg')
    rescan(source, media, db)
    original = {p.name: p.read_bytes() for p in media.iterdir() if p.is_file()}
    color = cover_pixel(source, 'embedded.flac')
    assert color[0] > 230 and 130 < color[1] < 190 and color[2] < 30
    monkeypatch.setattr(thumbnails, '_audio_cover', lambda _: (_ for _ in ()).throw(
        AssertionError('The repeated cover must use the existing cache')))
    assert cover_pixel(source, 'embedded.flac') == color
    assert original == {p.name: p.read_bytes() for p in media.iterdir() if p.is_file()}


def test_cover_revision_changes_without_audio_reads_and_bounds_candidates(art_cache):
    from PIL import Image
    from tests.browser_ost_fixture import write_audio
    source, _, media, db = art_cache
    ost.adopt(source.source_id)
    write_audio(media / 'sample.wav')
    Image.new('RGB', (40, 40), 'blue').save(media / 'cover.jpg')
    rescan(source, media, db)
    before = next(item for item in browse(source)['items'] if item['name'] == 'sample.wav')['cover_version']
    Image.new('RGB', (48, 48), 'red').save(media / 'cover.jpg')
    # The source index's integer timestamps have not been refreshed.
    with patch.object(Path, 'read_bytes', side_effect=AssertionError('Facts must not decode media')):
        after = next(item for item in browse(source)['items'] if item['name'] == 'sample.wav')['cover_version']
    assert before != after
    assert cover_pixel(source, 'sample.wav')[0] > 220
    for name in library.COVER_FILES:
        Image.new('RGB', (40, 40), 'blue').save(media / name)
    rescan(source, media, db)
    with index.open_index(db) as connection:
        paths = library.named_covers(connection, source, '', [])
    assert len(paths) <= 12
    assert [p.name.lower() for p in paths] == list(library.COVER_FILES)


def test_cover_guards_roles_paths_nested_sources_and_linked_art(art_cache, tmp_path):
    from PIL import Image
    source, connect, media, db = art_cache
    with pytest.raises(HTTPException) as error:
        router.cover(source.source_id, '', 300, None)
    assert error.value.status_code == 404
    ost.adopt(source.source_id)
    for path in ('../outside', '/outside', 'C:\\outside'):
        with pytest.raises(HTTPException) as error:
            router.cover(source.source_id, path, 300, None)
        assert error.value.status_code == 400
    with connect() as connection:
        sources.register_source(connection, media / 'Album 2', 'Own source')
    with pytest.raises(HTTPException) as error:
        router.cover(source.source_id, 'Album 2', 300, None)
    assert error.value.status_code == 403
    outside = tmp_path / 'private.png'
    Image.new('RGB', (40, 40), 'red').save(outside)
    (media / 'folder.png').symlink_to(outside)
    rescan(source, media, db)
    with pytest.raises(HTTPException) as error:
        router.cover(source.source_id, '', 300, None)
    assert error.value.status_code == 404
    with connect() as connection:
        sources.update_source(connection, source.source_id, visible=False)
    with pytest.raises(HTTPException) as error:
        router.cover(source.source_id, '', 300, None)
    assert error.value.status_code == 404
    assert outside.is_file()


def test_cover_does_not_borrow_from_parent_and_missing_art_is_a_deliberate_miss(art_cache):
    from PIL import Image
    from tests.browser_ost_fixture import write_audio
    source, _, media, db = art_cache
    ost.adopt(source.source_id)
    Image.new('RGB', (40, 40), 'blue').save(media / 'cover.jpg')
    write_audio(media / 'Album 2/曲 1.wav')
    rescan(source, media, db)
    for path in ('Album 2/曲 1.wav', 'Album 2', 'notes.md'):
        with pytest.raises(HTTPException) as error:
            router.cover(source.source_id, path, 300, None)
        assert error.value.status_code == 404


def seed_publisher_music(root, layout='publisher'):
    """Generate publisher-style originals with the already bundled FFmpeg."""
    import os
    import subprocess
    import imageio_ffmpeg
    from PIL import Image
    root.mkdir()
    (root / 'Scans').mkdir()
    Image.new('RGB', (48, 48), (20, 160, 40)).save(root / 'Scans/Booklet 01.png')
    Image.new('RGB', (72, 72), (20, 50, 220)).save(root / 'cover.jpg')
    (root / 'release.cue').write_text('TITLE "Publisher edition"\nFILE "original.flac" WAVE\n', encoding='utf-8')
    (root / 'rip.log').write_bytes(b'Original publisher log\r\nKeep exactly\r\n')
    (root / 'Read me 曲.txt').write_text('Original accompanying notes 한글', encoding='utf-8')
    (root / 'Bonus material').mkdir()
    paths = {'publisher': ['Disc A/01 - Intro 曲.flac', 'Disc B/01 - Finale.flac'],
             'flat': ['01 - Intro 曲.flac', '02 - Finale.flac'],
             'untagged': ['Part I/Track 02.flac', 'Part II/Track 10.flac']}[layout]
    for position, relative in enumerate(paths):
        target = root / relative; target.parent.mkdir(parents=True, exist_ok=True)
        command = [imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.15']
        if position == 0:
            command += ['-i', str(root / 'Scans/Booklet 01.png'), '-map', '0:a', '-map', '1:v',
                        '-c:v', 'copy', '-disposition:v', 'attached_pic']
        command += ['-c:a', 'flac', '-t', '0.15']
        if layout != 'untagged':
            command += ['-metadata', 'album=Publisher edition', '-metadata', 'albumartist=Original artist',
                '-metadata', 'title=' + ('Opening 曲' if position == 0 else 'Finale'),
                '-metadata', 'disc=' + str(position + 1 if layout == 'publisher' else 1),
                '-metadata', 'track=' + str(1 if layout == 'publisher' else position + 1)]
        subprocess.run([*command, str(target)], check=True, timeout=30,
            **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
    return paths


def publisher_music_tree(root):
    import hashlib
    return {path.relative_to(root).as_posix(): ('dir',) if path.is_dir() else
            ('file', hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            for path in root.rglob('*')}


@pytest.fixture
def publisher_music(catalog, monkeypatch, request):
    from types import SimpleNamespace
    import thumbnails
    _, connect, media, db = catalog
    layout = getattr(request, 'param', 'publisher')
    root = media.parent / '[Label] Album 曲 [FLAC]'
    paths = seed_publisher_music(root, layout)
    with connect() as connection:
        source = sources.register_source(connection, root, 'Publisher collection', role='ost')
    with index.open_index(db) as connection:
        index.scan_source(connection, source.source_id, root)
    cache = config.SUITE_HOME / 'cache/thumbnails'
    monkeypatch.setattr(thumbnails, 'THUMB_DIR', cache)
    monkeypatch.setattr(thumbnails, '_schedule_thumbnail_prune', lambda: None)
    before = publisher_music_tree(root)
    yield SimpleNamespace(source=source, connect=connect, root=root, db=db, paths=paths, layout=layout, cache=cache)
    assert publisher_music_tree(root) == before


@pytest.mark.parametrize('publisher_music', ['publisher', 'flat', 'untagged'], indirect=True)
def test_music_preservation_reads_scans_keep_publisher_tree_tags_and_extras(publisher_music):
    from starlette.responses import Response
    from services import audio_metadata
    case = publisher_music
    user_bytes = config.USER_DB_PATH.read_bytes()
    with patch.object(audio_metadata, '_extract', side_effect=AssertionError('Listing must not parse original tags')):
        assert {item['path'] for item in router.tracks(0, 240)['items']} == set(case.paths)
        assert browse(case.source)['total'] > 0
    for relative in case.paths:
        metadata = router.metadata(Response(), case.source.source_id, relative)
        assert metadata['status'] == 'ready' and metadata['duration'] > 0
        if case.layout != 'untagged':
            assert metadata['tags']['album'] == ['Publisher edition']
            assert metadata['tags']['albumartist'] == ['Original artist']
    cards = router.albums(0, 120)['items']
    assert len(cards) == (2 if case.layout == 'untagged' else 1)
    assert sum(card['total'] for card in cards) == 2
    assert {item['path'] for card in cards for item in router.album_tracks(case.source.source_id, card['anchor'], 0, 120)['items']} == set(case.paths)
    assert [item['name'] for item in router.tracks(0, 240)['items']] == [Path(path).name for path in case.paths]
    with index.open_index(case.db) as connection:
        assert connection.execute('SELECT count(*) FROM files_audio_metadata').fetchone()[0] == 2
        assert connection.execute('SELECT count(*) FROM ost_catalog').fetchone()[0] == 2
        index.scan_source(connection, case.source.source_id, case.root)
    for relative in case.paths:
        assert router.metadata(Response(), case.source.source_id, relative)['status'] == 'ready'
    assert config.USER_DB_PATH.read_bytes() == user_bytes
    assert not (case.root / 'local').exists() and not (case.root / '.keivotos').exists()


def test_music_preservation_cache_clear_keeps_original_embedded_and_external_art(publisher_music):
    from PIL import Image
    import thumbnails
    case = publisher_music
    external = router.cover(case.source.source_id, '', 300, None)
    embedded = router.cover(case.source.source_id, case.paths[0], 300, None)
    assert Path(external.path).resolve().is_relative_to(case.cache.resolve())
    assert Path(embedded.path).resolve().is_relative_to(case.cache.resolve())
    with Image.open(external.path) as picture: assert picture.convert('RGB').getpixel((0, 0))[2] > 200
    with Image.open(embedded.path) as picture: assert picture.convert('RGB').getpixel((0, 0))[1] > 140
    assert thumbnails.clear_thumbnail_cache() == 2
    assert not list(case.cache.glob('*.webp'))
    assert Path(router.cover(case.source.source_id, case.paths[0], 300, None).path).is_file()


def test_music_preservation_relocated_copy_keeps_source_and_authored_note(publisher_music):
    import hashlib
    import shutil
    from files_base import annotations
    from starlette.responses import Response
    case = publisher_music
    digest = hashlib.md5((case.root / case.paths[0]).read_bytes()).hexdigest()
    with case.connect() as connection:
        annotations.ensure_annotations_schema(connection)
        note = annotations.upsert_annotation(connection, subject_kind='file', content_hash=digest,
            source_id=case.source.source_id, relative_path=case.paths[0], description='My album note', author='My override')
    for relative in case.paths: router.metadata(Response(), case.source.source_id, relative)
    relocated = case.root.with_name('Relocated publisher collection')
    shutil.copytree(case.root, relocated)
    with case.connect() as connection:
        connection.execute('UPDATE files_sources SET path=?,display_name=? WHERE source_id=?',
                           (str(relocated), 'New display name', case.source.source_id)); connection.commit()
    with index.open_index(case.db) as connection:
        index.scan_source(connection, case.source.source_id, relocated)
    assert {item['path'] for item in router.tracks(0, 240)['items']} == set(case.paths)
    for relative in case.paths: assert router.metadata(Response(), case.source.source_id, relative)['status'] == 'ready'
    with case.connect() as connection:
        current = annotations.get_annotation(connection, content_hash=digest, source_id=case.source.source_id, relative_path=case.paths[0])
        assert (current.id, current.description, current.author) == (note.id, 'My album note', 'My override')
        assert sources.get_source(connection, case.source.source_id).path == str(relocated)
    assert publisher_music_tree(relocated) == publisher_music_tree(case.root)


def test_music_preservation_missing_hidden_and_nested_sources_still_refuse(publisher_music):
    from starlette.responses import Response
    case = publisher_music
    with case.connect() as connection:
        child = sources.register_source(connection, case.root / 'Disc A', 'Hidden child', role='files')
        sources.update_source(connection, child.source_id, visible=False)
    with pytest.raises(HTTPException) as denied: router.metadata(Response(), case.source.source_id, case.paths[0])
    assert denied.value.status_code == 403
    assert {item['path'] for item in router.tracks(0, 240)['items']} == {case.paths[1]}
    with case.connect() as connection: sources.update_source(connection, case.source.source_id, visible=False)
    assert router.tracks(0, 240)['items'] == []
    with pytest.raises(HTTPException) as denied: router.metadata(Response(), case.source.source_id, case.paths[1])
    assert denied.value.status_code == 404
    with case.connect() as connection:
        sources.update_source(connection, case.source.source_id, visible=True)
        connection.execute('UPDATE files_sources SET path=? WHERE source_id=?',
                           (str(case.root.with_name('Missing publisher collection')), case.source.source_id)); connection.commit()
    with pytest.raises(HTTPException) as denied: browse(case.source)
    assert denied.value.status_code == 404

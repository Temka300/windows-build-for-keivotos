"""Isolated Video discovery, ownership and diagnostic contracts."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from files_base import index, sources
from modules import video
from modules.video import router
import database


@pytest.fixture
def library(tmp_path, monkeypatch):
    user = tmp_path / 'user.sqlite'
    media = tmp_path / 'media'
    media.mkdir()
    for name in ['B.webm', 'a.MP4', '100%.m4v', 'ignored.txt', 'unsupported.mkv']:
        (media / name).write_bytes(b'fixture')
    with sqlite3.connect(user) as connection:
        connection.row_factory = sqlite3.Row
        sources.ensure_sources_schema(connection)
        source = sources.register_source(connection, media)
    db = tmp_path / 'files.sqlite'
    with index.open_index(db) as connection:
        index.scan_source(connection, source.source_id, media)
    @contextmanager
    def get_user_db():
        connection = sqlite3.connect(user)
        connection.row_factory = sqlite3.Row
        try: yield connection
        finally: connection.close()
    monkeypatch.setattr(router, 'get_user_db', get_user_db)
    monkeypatch.setattr(database, 'get_user_db', get_user_db)
    monkeypatch.setattr(router, 'FILES_DB_PATH', db)
    return source, get_user_db, media


def test_discovery_role_visibility_literal_search_and_pagination(library):
    source, connect, media = library
    assert router.library('', 0, 120)['total'] == 0
    video.adopt(source.source_id)
    assert router.library('', 0, 120)['total'] == 4
    assert router.library('%', 0, 120)['items'][0]['name'] == '100%.m4v'
    assert len(router.library('', 1, 2)['items']) == 2
    assert router.library('A.MP4', 0, 120)['total'] == 1
    with connect() as connection:
        sources.update_source(connection, source.source_id, visible=False)
    assert router.library('', 0, 120)['total'] == 0
    assert (media / 'a.MP4').read_bytes() == b'fixture'


def test_release_and_forget_only_change_registration(library):
    source, connect, media = library
    before = {p.name:p.read_bytes() for p in media.iterdir()}
    video.adopt(source.source_id)
    video.release(source.source_id)
    with connect() as connection: assert sources.get_source(connection, source.source_id).role == 'files'
    video.adopt(source.source_id)
    video.release(source.source_id, True)
    with connect() as connection: assert sources.get_source(connection, source.source_id) is None
    assert before == {p.name:p.read_bytes() for p in media.iterdir()}


def test_failure_log_requires_known_visible_video_and_contains_context(library, caplog):
    source, _, _ = library
    video.adopt(source.source_id)
    assert router.playback_error(router.PlaybackFailure(source_id=source.source_id, path='a.MP4', reason='decode')) == {'status':'logged'}
    assert 'a.MP4' in caplog.text and 'decode' in caplog.text
    for path in ['../outside.mp4', 'ignored.txt', '/unknown.mp4']:
        with pytest.raises(HTTPException) as error:
            router.playback_error(router.PlaybackFailure(source_id=source.source_id, path=path, reason='decode'))
        assert error.value.status_code == 404


def test_descriptor_uses_shared_index_and_has_no_new_durable_state(tmp_path):
    descriptor = video.descriptor(tmp_path, 'test')
    assert descriptor.database == tmp_path / 'base/files.sqlite'
    assert descriptor.backup_components() == ()
    assert descriptor.background_tasks() == []
    assert descriptor.disableable


def _random_video(source, path='', q='', category=None):
    from fastapi import Response
    response = Response()
    result = router.random_video(response, q, source.source_id, path, category)
    assert response.headers['cache-control'] == 'no-store'
    return result


def test_random_video_excludes_current_preserves_listing_and_empty_single(library, monkeypatch):
    source, _, media = library
    video.adopt(source.source_id)
    before = router.library('', 0, 120)
    original = {p.name: p.read_bytes() for p in media.iterdir()}
    monkeypatch.setattr(router.random, 'sample', lambda population, count: list(population))
    result = _random_video(source, '100%.m4v')
    assert result['item']['name'] == 'a.MP4'
    assert _random_video(source, '100%.m4v', '100%')['item'] is None
    assert _random_video(source, '', 'does not exist')['item'] is None
    assert router.library('', 0, 120) == before
    assert {p.name: p.read_bytes() for p in media.iterdir()} == original


def test_random_video_reaches_later_pages_and_respects_literal_search(library, monkeypatch):
    source, _, media = library
    video.adopt(source.source_id)
    for number in range(130):
        (media / f'queue {number:03}.mp4').write_bytes(b'generated indexed video')
    with index.open_index(router.FILES_DB_PATH) as connection:
        index.scan_source(connection, source.source_id, media)
    monkeypatch.setattr(router.random, 'sample', lambda population, count: list(reversed(population)))
    result = _random_video(source, 'queue 000.mp4', 'QUEUE ')
    assert result['offset'] == 129 and result['item']['path'] == 'queue 129.mp4'
    assert result['total'] == 130
    assert _random_video(source, '', '%')['item']['name'] == '100%.m4v'


def test_random_video_category_and_default_scope(library, monkeypatch):
    from uuid import uuid4
    source, connect, _ = library
    video.adopt(source.source_id)
    with connect() as connection:
        user = connection.execute('PRAGMA database_list').fetchone()[2]
        router.categories.publish(connection)
        category = router.categories.create(connection, '선택', str(uuid4()))
        router.categories.set_membership(connection, source.source_id, 'B.webm', [category['id']], 0,
                                        str(uuid4()), lambda *_: None)
    monkeypatch.setattr(router, 'USER_DB_PATH', Path(user))
    assert _random_video(source, '', category=category['id'])['item']['path'] == 'B.webm'
    assert _random_video(source, 'B.webm', category=category['id'])['item'] is None
    assert _random_video(source, '', category='default')['item']['path'] != 'B.webm'


@pytest.mark.parametrize('state', ['missing', 'directory', 'hidden', 'released', 'child', 'private', 'symlink'])
def test_random_video_rechecks_files_guards(library, monkeypatch, tmp_path, state):
    source, connect, media = library
    video.adopt(source.source_id)
    path = media / 'candidate.mp4'
    path.write_bytes(b'preserved candidate')
    with index.open_index(router.FILES_DB_PATH) as connection:
        index.scan_source(connection, source.source_id, media)
    if state in ('missing', 'directory', 'symlink'):
        path.rename(media / 'preserved-candidate.fixture')
        if state == 'directory':
            path.mkdir()
        elif state == 'symlink':
            outside = tmp_path / 'outside.mp4'; outside.write_bytes(b'outside guard fixture')
            try: path.symlink_to(outside)
            except OSError: pytest.skip('Creating a native symlink requires OS permission')
    elif state == 'hidden':
        with connect() as connection: sources.update_source(connection, source.source_id, visible=False)
    elif state == 'released': video.release(source.source_id)
    elif state == 'private': monkeypatch.setattr(router.config, 'SUITE_HOME', media)
    elif state == 'child':
        nested = media / 'nested'; nested.mkdir(); path.rename(nested / path.name)
        with index.open_index(router.FILES_DB_PATH) as connection:
            index.scan_source(connection, source.source_id, media)
        with connect() as connection: sources.register_source(connection, nested)
    assert _random_video(source, '', 'candidate')['item'] is None
    assert (media / 'preserved-candidate.fixture').read_bytes() == b'preserved candidate' if state in ('missing','directory','symlink') else True


def test_random_video_detects_changed_pagination(library, monkeypatch):
    source, _, _ = library; video.adopt(source.source_id)
    original = router.library; calls = []
    def changed(*args):
        result = original(*args); calls.append(args)
        if len(calls) > 1: result['total'] += 1
        return result
    monkeypatch.setattr(router, 'library', changed)
    monkeypatch.setattr(router.random, 'sample', lambda population, count: list(reversed(population)))
    with pytest.raises(HTTPException) as error: _random_video(source)
    assert error.value.status_code == 409


def test_random_video_api_validates_new_parameters(library):
    import asyncio
    from urllib.parse import urlencode
    from fastapi import FastAPI
    app = FastAPI(); app.include_router(router.router)
    async def request(params):
        messages = []
        async def receive(): return {'type':'http.request','body':b'','more_body':False}
        async def send(message): messages.append(message)
        await app({'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'GET',
                   'scheme':'http','path':'/api/video/random','raw_path':b'/api/video/random',
                   'query_string':urlencode(params).encode(),'root_path':'','headers':[],
                   'server':('fixture',80),'client':('127.0.0.1',1)},receive,send)
        return next(message['status'] for message in messages if message['type']=='http.response.start')
    async def verify():
        for params in ({'q':'x'*257}, {'current_source_id':'x'*257}, {'current_path':'x'*4097}, {'category':'x'*37}, {'series_source_id':'x'*257,'series_path':'series'}, {'series_source_id':'source','series_path':'x'*4097}, {'series_source_id':'source'}): assert await request(params) == 422
    asyncio.run(verify())

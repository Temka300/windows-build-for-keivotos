"""Video series browsing uses isolated sources/indexes and never rewrites media."""
from contextlib import contextmanager, closing
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, Response
import asyncio
import json
from urllib.parse import urlencode, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import config
from files_base import index, sources
from modules.video import router


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    home = tmp_path / 'home'; home.mkdir()
    media = tmp_path / 'media'; media.mkdir()
    user = home / 'user.sqlite'; db = home / 'files.sqlite'
    @contextmanager
    def connect():
        with closing(sqlite3.connect(user)) as connection:
            connection.row_factory = sqlite3.Row
            with connection:
                yield connection
    with connect() as connection:
        sources.ensure_sources_schema(connection)
        source = sources.register_source(connection, media, role='video')
        router.categories.publish(connection)
    monkeypatch.setattr(router, 'get_user_db', connect)
    monkeypatch.setattr(router, 'FILES_DB_PATH', db)
    monkeypatch.setattr(router, 'USER_DB_PATH', user)
    monkeypatch.setattr(config, 'SUITE_HOME', home)
    names = ['film10.mp4', 'film2.mp4', 'Series 2/episode 0.mp4',
             'Series 2/Season 10/episode 1.mp4', 'Series 2/Season 2/episode 10.mp4',
             'Series 2/Season 2/episode 2.mp4', 'Series 2/Specials/extra.webm',
             'Series 10/episode 1.mkv', 'Series 2/cover.jpg', 'Series 2/poster.png',
             'Straße/한국어 1.mp4', '100%_ collection/episode.mp4']
    for name in names:
        path = media / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(name.encode())
    with index.open_index(db) as connection: index.scan_source(connection, source.source_id, media)
    return source, media, connect, db


def listing(q='', offset=0, limit=120, category=None):
    response = Response(); result = router.catalog(response, q, offset, limit, category)
    assert response.headers['cache-control'] == 'no-store'
    return result


def detail(source, path='Series 2', kind='series', q='', offset=0, limit=120, category=None):
    response = Response(); result = router.episodes(response, source.source_id, path, kind, q, offset, limit, category)
    assert response.headers['cache-control'] == 'no-store'
    return result


def test_standalone_series_real_seasons_natural_order_and_local_cover(catalog):
    source, media, _, _ = catalog
    before = {p.relative_to(media).as_posix(): p.read_bytes() for p in media.rglob('*') if p.is_file()}
    titles = listing()['items']
    assert [item['name'] for item in titles] == ['100%_ collection', 'film2.mp4', 'film10.mp4', 'Series 2', 'Series 10', 'Straße']
    series = next(item for item in titles if item['name'] == 'Series 2')
    assert series['kind'] == 'series' and series['episode_count'] == 5
    assert series['cover']['path'] == 'Series 2/cover.jpg'
    assert 'episodes' not in series
    rows = detail(source)['items']
    assert [row['name'] for row in rows] == ['episode 0.mp4', 'episode 2.mp4', 'episode 10.mp4', 'episode 1.mp4', 'extra.webm']
    assert [row['section'] for row in rows] == ['', 'Season 2', 'Season 2', 'Season 10', 'Specials']
    assert detail(source, offset=2, limit=1)['items'] == [rows[2]]
    assert detail(source, offset=99)['items'] == []
    single = detail(source, 'film2.mp4', 'video')
    assert single['total'] == 1 and single['items'][0]['path'] == 'film2.mp4'
    assert before == {p.relative_to(media).as_posix(): p.read_bytes() for p in media.rglob('*') if p.is_file()}


def test_cover_falls_back_to_first_video_and_rechecks_missing_local_art(catalog):
    source, media, _, _ = catalog
    (media / 'Series 2/cover.jpg').rename(media / 'preserved-cover.fixture')
    result = next(item for item in listing()['items'] if item['name'] == 'Series 2')
    assert result['cover']['path'] == 'Series 2/poster.png'
    (media / 'Series 2/poster.png').rename(media / 'preserved-poster.fixture')
    result = next(item for item in listing()['items'] if item['name'] == 'Series 2')
    assert result['cover']['path'] == 'Series 2/episode 0.mp4'
    assert result['episode_count'] == 5


@pytest.mark.parametrize('q,expected,count', [('STRASSE', 'Straße', 1), ('한국어', 'Straße', 1),
    ('%_', '100%_ collection', 1), ('season 2', 'Series 2', 2), ('EPISODE 10', 'Series 2', 1)])
def test_literal_unicode_series_folder_and_episode_search(catalog, q, expected, count):
    source, _, _, _ = catalog
    titles = listing(q)['items']; assert len(titles) == 1 and titles[0]['name'] == expected
    assert detail(source, titles[0]['path'], titles[0]['kind'], q)['total'] == count


def test_categories_filter_existing_episode_members_before_grouping(catalog):
    source, _, connect, _ = catalog
    with connect() as connection:
        first = router.categories.create(connection, 'First', str(uuid4()))
        second = router.categories.create(connection, 'Second', str(uuid4()))
        router.categories.set_membership(connection, source.source_id, 'Series 2/Season 2/episode 2.mp4',
            [first['id'], second['id']], 0, str(uuid4()), lambda *_: None)
        before = connection.execute('SELECT * FROM video_category_memberships').fetchall()
    for category in (first['id'], second['id']):
        titles = listing(category=category)
        assert titles['total'] == 1 and titles['items'][0]['episode_count'] == 1
        assert detail(source, category=category)['items'][0]['path'] == 'Series 2/Season 2/episode 2.mp4'
        assert listing('season 10', category=category)['total'] == 0
    assert detail(source, category='default')['total'] == 4
    assert listing('unknown')['total'] == 0
    with connect() as connection:
        assert connection.execute('SELECT * FROM video_category_memberships').fetchall() == before
        assert all(row['path'] != 'Series 2' for row in before)
    with pytest.raises(HTTPException) as failure: listing(category=str(uuid4()))
    assert failure.value.status_code == 404


def test_duplicate_names_stay_separate_across_sources_and_exact_prefixes(catalog, tmp_path):
    source, media, connect, db = catalog
    other = tmp_path / 'other'; (other / 'Series 2').mkdir(parents=True)
    (other / 'Series 2/episode 1.mp4').write_bytes(b'other source')
    (media / 'Series 2-extra').mkdir(); (media / 'Series 2-extra/episode 1.mp4').write_bytes(b'sibling')
    with connect() as connection: second = sources.register_source(connection, other, role='video')
    with index.open_index(db) as connection:
        index.scan_source(connection, source.source_id, media); index.scan_source(connection, second.source_id, other)
    assert len([item for item in listing()['items'] if item['name'] == 'Series 2']) == 2
    assert detail(source)['total'] == 5
    assert detail(second)['total'] == 1


@pytest.mark.parametrize('state', ['hidden', 'released', 'child', 'private', 'missing', 'escape'])
def test_fresh_ownership_and_file_guards_exclude_stale_index_rows(catalog, tmp_path, monkeypatch, state):
    source, media, connect, _ = catalog
    if state == 'hidden':
        with connect() as connection: sources.update_source(connection, source.source_id, visible=False)
    elif state == 'released':
        with connect() as connection: sources.update_source(connection, source.source_id, role='files')
    elif state == 'child':
        with connect() as connection: sources.register_source(connection, media / 'Series 2', role='files')
    elif state == 'private': monkeypatch.setattr(config, 'SUITE_HOME', media / 'Series 2')
    else:
        saved = media / 'preserved-series'; (media / 'Series 2').rename(saved)
        if state == 'escape':
            outside = tmp_path / 'outside'; outside.mkdir(); (outside / 'episode 0.mp4').write_bytes(b'outside')
            try: (media / 'Series 2').symlink_to(outside, target_is_directory=True)
            except OSError: pytest.skip('Native OS does not permit symlink creation')
    assert all(item['path'] != 'Series 2' for item in listing()['items'])
    with pytest.raises(HTTPException) as failure: detail(source)
    assert failure.value.status_code == 404


def test_http_bounds_unknown_groups_and_scope_pair_validation(catalog):
    source, _, _, _ = catalog
    app = FastAPI(); app.include_router(router.router)
    def request(endpoint, **params):
        url = urlsplit(endpoint + ('?' + urlencode(params) if params else ''))
        async def run():
            messages = []
            async def receive(): return {'type':'http.request', 'body':b'', 'more_body':False}
            async def send(message): messages.append(message)
            await app({'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'GET',
                       'scheme':'http','path':url.path,'raw_path':url.path.encode(),'query_string':url.query.encode(),
                       'headers':[],'client':('127.0.0.1',1),'server':('test',80)},receive,send)
            start = next(m for m in messages if m['type']=='http.response.start')
            return start['status'], dict(start['headers'])
        return asyncio.run(run())
    assert request('/api/video/catalog', limit=0)[0] == 422
    assert request('/api/video/catalog', q='a' * 257)[0] == 422
    for path in ('../outside', '/absolute', 'Series 2-extra', 'Series 2/Season 2'):
        assert request('/api/video/episodes', source_id=source.source_id, path=path)[0] == 404
    assert request('/api/video/episodes', source_id=source.source_id, path='Series 2', kind='wrong')[0] == 422
    assert request('/api/video/random', series_path='Series 2')[0] == 422
    status, headers = request('/api/video/catalog')
    assert status == 200 and headers[b'cache-control'] == b'no-store'


def test_series_random_stays_in_filtered_natural_queue_and_legacy_stays_flat(catalog, monkeypatch):
    source, _, _, _ = catalog
    monkeypatch.setattr(router.random, 'sample', lambda population, count: list(reversed(population)))
    flat = router.library('', 0, 120)
    result = router.random_video(Response(), 'season 2', source.source_id, 'Series 2/Season 2/episode 2.mp4',
                                 None, source.source_id, 'Series 2')
    assert result['item']['path'] == 'Series 2/Season 2/episode 10.mp4'
    assert result['offset'] == 1 and result['total'] == 2
    assert router.library('', 0, 120) == flat
    result = router.random_video(Response(), '', source.source_id, '', None, source.source_id, 'Series 10')
    assert result['item'] is None  # The only episode has an unsupported container.


def test_valid_title_can_have_zero_matching_category_episodes(catalog):
    source, _, connect, _ = catalog
    with connect() as connection: category = router.categories.create(connection, 'Empty', str(uuid4()))
    assert detail(source, category=category['id']) == {'items': [], 'total': 0}
    assert detail(source, q='no matching episode') == {'items': [], 'total': 0}


def test_unicode_numeric_names_and_catalog_paging_never_read_media(catalog, monkeypatch):
    source, media, _, db = catalog
    (media / '²').mkdir(); (media / '²/episode.mp4').write_bytes(b'preserved unicode file')
    for number in range(125): (media / f'Paged {number}.mp4').write_bytes(b'fixture')
    with index.open_index(db) as connection: index.scan_source(connection, source.source_id, media)
    def forbidden(*args, **kwargs): raise AssertionError('Browsing read media content')
    monkeypatch.setattr(Path, 'read_bytes', forbidden)
    assert listing('²')['items'][0]['name'] == '²'
    assert listing('Paged', offset=120)['total'] == 125
    assert [row['name'] for row in listing('Paged', offset=120)['items']] == [f'Paged {n}.mp4' for n in range(120,125)]

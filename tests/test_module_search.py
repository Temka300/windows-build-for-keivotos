"""Local search contracts across existing owners; no worker or provider traffic."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import config
import database
from files_base import index, sources
from routers import files
from modules.video import router as video
from tests.test_youtube_module import local as youtube_local


@pytest.fixture
def search_catalog(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    media = tmp_path / 'media'
    media.mkdir()
    user = home / 'user.sqlite'
    db = home / 'files.sqlite'
    @contextmanager
    def connect():
        with sqlite3.connect(user) as connection:
            connection.row_factory = sqlite3.Row
            yield connection
    with connect() as connection:
        sources.ensure_sources_schema(connection)
        source = sources.register_source(connection, media, role='video')
    for owner in (files, video, database):
        monkeypatch.setattr(owner, 'get_user_db', connect)
    monkeypatch.setattr(config, 'SUITE_HOME', home)
    monkeypatch.setattr(config, 'FILES_DB_PATH', db)
    monkeypatch.setattr(video, 'FILES_DB_PATH', db)
    monkeypatch.setattr(video, 'USER_DB_PATH', user)
    return source, media, connect, db


def scan(catalog):
    source, media, _, db = catalog
    with index.open_index(db) as connection:
        index.scan_source(connection, source.source_id, media)


@pytest.mark.parametrize('query,expected', [('école','ÉCOLE.mp4'), ('STRASSE','Straße.mp4'),
    ('한국어','한국어.mp4'), ('%_','100%_.mp4'), ('\\','literal\\name.mp4')])
def test_literal_unicode_search_keeps_each_existing_owner(search_catalog, query, expected):
    source, media, _, _ = search_catalog
    # Backslash is a filename on POSIX; Windows reserves it as a separator.
    if '\\' in expected and sys.platform == 'win32':
        pytest.skip('Backslash filename is POSIX-only')
    before = {name: name.encode() for name in ['ÉCOLE.mp4','Straße.mp4','한국어.mp4','100%_.mp4','ordinary.mp4',expected]}
    for name, data in before.items():
        (media / name).write_bytes(data)
    scan(search_catalog)
    assert [row.name for row in files.search(query, source.source_id, 200)] == [expected]
    result = video.library(query, 0, 120)
    assert result['total'] == 1
    assert [row['name'] for row in result['items']] == [expected]
    assert before == {name: (media / name).read_bytes() for name in before}


def test_registered_child_ownership_applies_before_count_and_paging(search_catalog):
    source, media, connect, db = search_catalog
    child_path = media / 'Private_%'
    child_path.mkdir()
    for n in range(5):
        (child_path / f'match-{n}.mp4').write_bytes(b'preserved child')
    (media / 'match-z.mp4').write_bytes(b'preserved parent')
    scan(search_catalog)  # Deliberately retain parent rows from before registration.
    with connect() as connection:
        child = sources.register_source(connection, child_path, role='video')
        sources.update_source(connection, child.source_id, visible=False)
    with index.open_index(db) as connection:
        index.scan_source(connection, child.source_id, child_path)
        saved_count = connection.execute('SELECT count(*) FROM files_index').fetchone()[0]
    assert [row.name for row in files.search('match', source.source_id, 1)] == ['match-z.mp4']
    assert files.search('match', child.source_id, 200) == []
    assert [row.name for row in files.search('match', None, 200)] == ['match-z.mp4']
    assert video.library('match', 0, 1)['total'] == 1
    with connect() as connection:
        sources.update_source(connection, child.source_id, visible=True)
    found = files.search('match', source.source_id, 200)
    assert len(found) == 6 and sum(row.source_id == source.source_id for row in found) == 1
    assert video.library('match', 0, 1)['total'] == 6
    with connect() as connection:
        sources.update_source(connection, child.source_id, role='files')
    assert video.library('match', 0, 120)['total'] == 1
    with index.open_index(db) as connection:
        assert connection.execute('SELECT count(*) FROM files_index').fetchone()[0] == saved_count
    assert (child_path / 'match-0.mp4').read_bytes() == b'preserved child'


def test_missing_and_unregistered_sources_keep_index_without_search_results(search_catalog):
    source, media, connect, db = search_catalog
    (media / 'preserved.mp4').write_bytes(b'preserved')
    scan(search_catalog)
    moved = media.with_name('offline')
    media.rename(moved)
    assert files.search('preserved', None, 200) == []
    assert video.library('preserved', 0, 120) == {'items': [], 'total': 0}
    moved.rename(media)
    with connect() as connection:
        sources.remove_source(connection, source.source_id)
    assert files.search('preserved', None, 200) == []
    with index.open_index(db) as connection:
        assert connection.execute('SELECT count(*) FROM files_index').fetchone()[0] == 1
    assert (media / 'preserved.mp4').read_bytes() == b'preserved'


def test_search_has_stable_later_pages_and_keeps_literal_punctuation(search_catalog):
    source, media, _, db = search_catalog
    for n in range(215):
        folder = media / str(n % 3)
        folder.mkdir(exist_ok=True)
        (folder / f'match-{n:03d}.mp4').write_bytes(b'x')
    scan(search_catalog)
    all_rows = files.search('match-', source.source_id, 1000, offset=0)
    first = files.search('match-', source.source_id, 200, offset=0)
    second = files.search('match-', source.source_id, 200, offset=200)
    assert first + second == all_rows and len(second) == 15
    assert files.search('match-', source.source_id, 200, offset=1000) == []
    with index.open_index(db) as connection:
        assert index.search_by_name(connection, 'match-', source_ids=[]) == []
    assert video.library('match-', 200, 120)['total'] == 215
    assert len(video.library('match-', 200, 120)['items']) == 15


@pytest.mark.parametrize('query,title', [('école','ÉCOLE'), ('STRASSE','Straße'), ('한국어','한국어 100%_'), ('%_','한국어 100%_')])
def test_youtube_title_search_uses_unicode_in_operational_rows(youtube_local, query, title):
    from modules.youtube import jobs, router
    source, _, _ = youtube_local
    for n, name in enumerate(['ÉCOLE','Straße','한국어 100%_','ordinary']):
        job = jobs.create('https://www.youtube.com/watch?v=' + f'{n:011d}', source.source_id)
        with jobs.get_jobs_db() as connection:
            connection.execute('UPDATE youtube_downloads SET title=? WHERE id=?', (name, job['id']))
            connection.commit()
    found = router.library(query, 0, 1)
    assert found['total'] == 1
    assert [item['title'] for item in found['items']] == [title]
    assert router.library(query, 1, 1)['items'] == []


def test_youtube_saved_only_search_shares_unicode_predicate_and_keeps_facts(youtube_local):
    from modules.youtube import jobs, router, user_schema
    from services import item_catalog
    source, media, connect = youtube_local
    pending = jobs.create('https://www.youtube.com/watch?v=abcdefghijk', source.source_id)
    record = dict(pending, id='saved-only', title='ÉCOLE Straße 한국어 100%_', path='YouTube/saved-only/saved.mp4', thumbnail='')
    (media / 'YouTube/saved-only').mkdir(parents=True)
    (media / record['path']).write_bytes(b'preserved completed save')
    with connect() as connection:
        item_catalog.ensure_catalog_schema(connection)
        item_catalog.ensure_library(connection, namespace='files.source', legacy_key=source.source_id,
            library_id=source.source_id, kind='linked', display_name='Saved')
        connection.commit()
        connection.execute('BEGIN')
        user_schema.register_catalog_publication(connection, record, legacy_job_ids=['saved-only'],
            source_id=source.source_id, source_root=media, forbidden_roots=[jobs.config.SUITE_HOME])
        connection.commit()
        before = [tuple(row.values()) for row in connection.execute('SELECT * FROM kv_item_captures ORDER BY item_id,namespace,capture_key')]
    for term in ('école','STRASSE','한국어','%_'):
        result = router.library(term, 0, 1)
        assert result['total'] == 1 and result['items'][0]['id'] == 'saved-only'
    with connect() as connection:
        assert [tuple(row.values()) for row in connection.execute('SELECT * FROM kv_item_captures ORDER BY item_id,namespace,capture_key')] == before
    assert (media / record['path']).read_bytes() == b'preserved completed save'


def test_changed_source_location_never_searches_the_previous_root(search_catalog):
    source, media, connect, db = search_catalog
    (media / 'previous.mp4').write_bytes(b'keep previous bytes')
    scan(search_catalog)
    replacement = media.with_name('replacement')
    replacement.mkdir()
    (replacement / 'new.mp4').write_bytes(b'new fixture')
    with connect() as connection:
        connection.execute('UPDATE files_sources SET path=? WHERE source_id=?', (str(replacement), source.source_id))
        connection.commit()
    assert files.search('previous', source.source_id, 200) == []
    assert video.library('previous', 0, 120)['total'] == 0
    with index.open_index(db) as connection:
        assert connection.execute('SELECT count(*) FROM files_index').fetchone()[0] == 1
    assert (media / 'previous.mp4').read_bytes() == b'keep previous bytes'

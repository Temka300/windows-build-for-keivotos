"""Explicit Music metadata operations use only disposable profiles and audio."""
from pathlib import Path
import asyncio
import hashlib
import sqlite3
from unittest.mock import patch
import pytest
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import config
import database
import suite_modules
from files_base import index, sources, serving
from modules.ost import metadata_jobs as jobs, router, albums, library
from services import operations, folder_roles
from tests.browser_ost_fixture import write_audio


@pytest.fixture
def music(tmp_path, monkeypatch):
    media = tmp_path / 'Publisher'; media.mkdir()
    for path in ['Disc 1/a.wav','Disc 1/b.wav','Disc 2/c.wav']:
        write_audio(media/path)
    home=tmp_path/'profile';home.mkdir()
    monkeypatch.setattr(config,'SUITE_HOME',home)
    monkeypatch.setattr(config,'FILES_DB_PATH',home/'index.sqlite')
    monkeypatch.setattr(config,'JOBS_DB_PATH',home/'jobs.sqlite')
    monkeypatch.setattr(database,'USER_DB_PATH',home/'user.sqlite')
    monkeypatch.setattr(folder_roles,'FILES_DB_PATH',config.FILES_DB_PATH)
    monkeypatch.setattr(folder_roles,'SUITE_HOME',home)
    monkeypatch.setattr(router,'get_user_db',database.get_user_db)
    monkeypatch.setattr(jobs,'get_user_db',database.get_user_db)
    from routers import files
    monkeypatch.setattr(files,'get_user_db',database.get_user_db)
    monkeypatch.setattr(folder_roles,'get_user_db',database.get_user_db)
    with database.get_user_db() as connection:
        sources.ensure_sources_schema(connection);suite_modules.ensure_schema(connection)
        suite_modules.set_enabled(connection,'ost',True)
        source=sources.register_source(connection,media,'Publisher Music',role='ost')
    with index.open_index(config.FILES_DB_PATH) as connection:
        index.scan_source(connection,source.source_id,media)
    jobs.startup()
    yield source,media
    jobs._stop.set()


def test_request_deduplication_stored_progress_and_no_browse_decoding(music):
    source,media=music
    original={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in media.rglob('*.wav')}
    first=jobs.request(source.source_id)
    assert jobs.request(source.source_id)['id']==first['id']
    assert first['total'] is None and first['state']=='requested'
    assert jobs.run_next()
    status=jobs.listing()[0]
    assert status['processed']==status['total']==3 and status['progress']==100 and status['state']=='completed'
    with patch.object(jobs.audio_metadata,'_extract',side_effect=AssertionError('Navigation must not decode tags')):
        for _ in range(3):
            assert router.tracks(0,120)['total']==3
            cards=router.albums(0,120)['items'];assert cards
            for card in cards:
                router.album_tracks(source.source_id,card['anchor'],0,120)
    assert original=={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in original}
    with database.get_jobs_db() as connection:
        assert operations.get(connection,'ost',first['id'])['attempt']==1
    assert not jobs.run_next()


def test_files_and_suite_rescans_trigger_one_owner_operation(music, monkeypatch):
    source,_=music
    from routers import files
    monkeypatch.setattr(files,'get_user_db',database.get_user_db)
    result=files.scan_source(source.source_id)
    assert result.files==3
    job=jobs.listing()[0]
    assert not job['discover'] and job['state']=='requested'
    folder_roles.rescan(source.source_id)
    assert len(jobs.listing())==1
    jobs.run_next();assert jobs.listing()[0]['state']=='completed'


def test_refresh_is_selected_subtree_and_registered_children_are_excluded(music):
    source,media=music
    with database.get_user_db() as connection:
        child=sources.register_source(connection,media/'Disc 2','Separate',role='files')
        sources.update_source(connection,child.source_id,visible=False)
    with patch.object(jobs.audio_metadata,'read',wraps=jobs.audio_metadata.read) as reader:
        jobs.request(source.source_id,'Disc 1');jobs.run_next()
        assert {call.args[2] for call in reader.call_args_list}=={'Disc 1/a.wav','Disc 1/b.wav'}
    assert jobs.listing()[0]['total']==2
    with pytest.raises(serving.ServeDenied):jobs.request(source.source_id,'Disc 2')
    with pytest.raises(serving.ServeDenied):jobs.request(source.source_id,'../outside')


def test_failure_warning_retry_and_interrupted_restart(music):
    source,_=music
    job=jobs.request(source.source_id)
    jobs._stop.set();jobs.run_next()
    assert jobs.listing()[0]['state']=='interrupted'
    jobs.startup();retry=jobs.request(source.source_id)
    assert retry['id']!=job['id']
    with patch.object(jobs.audio_metadata,'read',return_value=jobs.audio_metadata.fallback('file.wav','limited')):
        jobs.run_next()
    result=next(row for row in jobs.listing() if row['id']==retry['id'])
    assert result['failed']==3 and result['state']=='completed-with-warnings'
    other=jobs.request(source.source_id)
    with database.get_jobs_db() as connection:
        operations.claim(connection,'ost',other['id']);connection.commit()
    jobs.startup()
    assert next(row for row in jobs.listing() if row['id']==other['id'])['state']=='interrupted'


def test_hidden_released_and_moved_sources_do_not_publish_stale_details(music):
    source,media=music
    jobs.request(source.source_id)
    with database.get_user_db() as connection:sources.update_source(connection,source.source_id,visible=False)
    jobs.run_next();assert jobs.listing()==[]
    with database.get_user_db() as connection:sources.update_source(connection,source.source_id,visible=True)
    assert jobs.listing()[0]['state']=='failed'
    with database.get_user_db() as connection:sources.update_source(connection,source.source_id,role='files')
    assert jobs.listing()==[]


def test_worker_survives_source_lock_and_stops_after_pending_reader(music):
    source,_=music;jobs.request(source.source_id)
    asyncio.run(_worker_lifecycle())


async def _worker_lifecycle():
    task=asyncio.create_task(jobs.worker())
    for _ in range(100):
        if jobs.listing()[0]['state']=='completed':break
        await asyncio.sleep(.03)
    assert jobs.listing()[0]['state']=='completed'
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task


def test_explicit_refresh_replaces_stored_presentation_with_unchanged_file_identity(music):
    source,_=music
    jobs.request(source.source_id);jobs.run_next()
    original=jobs.audio_metadata._extract
    def revised(*args):
        result=original(*args)
        result['display_title']='Refreshed title'
        return result
    with patch.object(jobs.audio_metadata,'_extract',side_effect=revised):
        jobs.request(source.source_id,'Disc 1');jobs.run_next()
    assert router.tracks(0,120,'Refreshed title')['total']==2
    assert router.tracks(0,120)['total']==3


def test_source_hidden_during_read_cannot_publish_its_result(music):
    source,_=music
    jobs.request(source.source_id)
    original=jobs.audio_metadata.read
    def hidden(*args,**kwargs):
        result=original(*args,**kwargs)
        with database.get_user_db() as connection:
            sources.update_source(connection,source.source_id,visible=False)
        return result
    with patch.object(jobs.audio_metadata,'read',side_effect=hidden):jobs.run_next()
    assert jobs.listing()==[]
    with index.open_index(config.FILES_DB_PATH) as connection:
        assert all(not row[0] for row in connection.execute('SELECT identity FROM ost_catalog'))


def test_idle_worker_never_reserves_folder_work(music):
    with patch.object(index,'source_work',side_effect=AssertionError('Idle worker must not block folder edits')):
        assert jobs.run_next() is False

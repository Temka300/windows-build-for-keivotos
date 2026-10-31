"""End-to-end owner recovery with generated files; no provider requests."""
import asyncio
from contextlib import contextmanager, closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from uuid import uuid4
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from services import operations
from files_base import sources
from modules.youtube import jobs as youtube_jobs, router as youtube_router, playlists
from modules.twitter import jobs as twitter_jobs, router as twitter_router, archive
from modules.danbooru import tools as danbooru_tools, operational_state as danbooru_state
from tests.test_youtube_module import local
from tests.test_twitter_module import catalog as twitter_catalog, write_capture, new_job

def archive_history(path):
    # Preserve generated history rather than deleting or rewriting it.
    retained=path.with_name('retained-'+uuid4().hex+'.sqlite')
    path.rename(retained)
    return retained

def test_youtube_request_replay_survives_missing_job_history(local,monkeypatch):
    source,media,legacy=local
    token=uuid4()
    job=youtube_jobs.create('https://youtu.be/abcdefghijk',source.source_id,request_id=token)
    assert youtube_jobs.create(job['url'],source.source_id,request_id=token)['id']==job['id']
    youtube_jobs.update(job['id'],status='downloading')
    stage=media/'.keivotos/youtube'/job['id'];stage.mkdir(parents=True)
    original=b'original local media'
    (stage/'video.mp4').write_bytes(original)
    youtube_jobs.publish(job,stage,{'path':'video.mp4','title':'Preserved'})
    with legacy() as db:
        playlist=playlists.create(db,'Saved collection',str(uuid4()))
        playlists.set_membership(db,job['id'],[playlist['id']],0)
    saved=youtube_jobs.get(job['id'])
    retained=archive_history(youtube_jobs.config.JOBS_DB_PATH)
    youtube_jobs._pending.clear()
    monkeypatch.setattr(youtube_jobs,'dependencies',lambda:{'ready':False,'missing':['fixture']})
    acknowledged=youtube_jobs.create(job['url'],source.source_id,request_id=token)
    assert acknowledged['id']==job['id'] and acknowledged['status']=='complete'
    assert (media/saved['path']).read_bytes()==original
    assert retained.exists()
    assert youtube_router.library('',0,60)['total']==1
    with legacy() as db:
        assert playlists.items(db,playlist['id'],0,60)['items'][0]['available']
        assert db.execute('SELECT count(*) AS n FROM youtube_downloads').fetchone()['n']==0
    with youtube_jobs.get_jobs_db() as db:
        assert db.execute('SELECT count(*) AS n FROM youtube_downloads').fetchone()['n']==0

def test_youtube_crash_between_catalog_commit_and_ack_repairs_locally(local,monkeypatch):
    source,media,legacy=local
    job=youtube_jobs.create('https://youtu.be/abcdefghijk',source.source_id,request_id=uuid4())
    youtube_jobs.update(job['id'],status='downloading')
    stage=media/'.keivotos/youtube'/job['id'];stage.mkdir(parents=True)
    (stage/'video.mp4').write_bytes(b'keep published bytes')
    original=youtube_jobs.update
    def lost_ack(identity,**values):
        if values.get('status')=='complete':raise OSError('acknowledgement interrupted')
        original(identity,**values)
    monkeypatch.setattr(youtube_jobs,'update',lost_ack)
    with pytest.raises(OSError,match='acknowledgement interrupted'):
        youtube_jobs.publish(job,stage,{'path':'video.mp4','title':'Saved before reply'})
    with youtube_jobs.get_jobs_db() as db:
        row=operations.get(db,'youtube',job['id'])
        assert row['publication']=='catalog-registered' and row['state']=='working'
    youtube_jobs.recover()
    monkeypatch.setattr(youtube_jobs,'update',original)
    monkeypatch.setattr(youtube_jobs,'dependencies',lambda:pytest.fail('Local repair attempted acquisition'))
    replacement=youtube_jobs.retry(job['id'])
    youtube_jobs.update(replacement['id'],status='downloading')
    youtube_jobs.resume_registration(replacement)
    saved=youtube_jobs.get(replacement['id'])
    assert saved['status']=='complete'
    assert (media/saved['path']).read_bytes()==b'keep published bytes'
    assert youtube_router.library('',0,60)['total']==1

def test_twitter_completed_request_and_archive_outlive_jobs(twitter_catalog,monkeypatch):
    source,media,_,legacy=twitter_catalog
    job=new_job(twitter_catalog)
    path=write_capture(media,post_id='333')
    twitter_jobs.update(job['id'],status='running',path=path,capture_id=archive._path_parts(path)[1])
    twitter_jobs.register(twitter_jobs.get(job['id']))
    before={p.relative_to(media).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in media.rglob('*') if p.is_file()}
    archive_history(twitter_jobs.config.JOBS_DB_PATH)
    monkeypatch.setattr(twitter_jobs,'dependencies',lambda:pytest.fail('Completed replay attempted acquisition'))
    acknowledged=twitter_jobs.create(job['url'],source.source_id,job['request_id'])
    assert acknowledged['status']=='complete' and acknowledged['id']==job['id']
    assert twitter_router.library(0,120,'','')['items']
    assert before=={p.relative_to(media).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in media.rglob('*') if p.is_file()}
    with twitter_jobs.get_jobs_db() as db:
        assert db.execute('SELECT count(*) AS n FROM twitter_downloads').fetchone()['n']==0

def test_twitter_late_old_attempt_cannot_change_retried_owner_row(twitter_catalog):
    job=new_job(twitter_catalog)
    twitter_jobs.update(job['id'],status='running')
    with twitter_jobs.get_jobs_db() as db:
        old=db.execute("SELECT token FROM kv_job_attempts WHERE owner='twitter'").fetchone()['token']
    twitter_jobs.update(job['id'],status='failed')
    twitter_jobs.retry(job['id'],uuid4())
    twitter_jobs.update(job['id'],status='running')
    before=twitter_jobs.get(job['id'])
    with operations.bind('twitter',job['id'],old):
        with pytest.raises(operations.StaleAttempt):
            twitter_jobs.update(job['id'],status='complete',path='obsolete/manifest.json')
    assert twitter_jobs.get(job['id'])==before

def test_danbooru_disable_drains_owned_process_and_persists_interruption(tmp_path,monkeypatch):
    import config
    monkeypatch.setattr(config,'JOBS_DB_PATH',tmp_path/'state/jobs.sqlite')
    identity='item03-drain-'+uuid4().hex[:8]
    result=danbooru_tools._launch_tool(identity,[[sys.executable,'-u','-c',"import time;print('PROGRESS:1/10',flush=True);time.sleep(60)"]],environment={})
    assert result['status']=='started'
    deadline=time.monotonic()+5
    while identity not in danbooru_tools._running_processes:
        if time.monotonic()>deadline:pytest.fail('Fixture child did not start')
        time.sleep(.01)
    process=danbooru_tools._running_processes[identity]
    asyncio.run(danbooru_tools.shutdown())
    assert process.poll() is not None and identity not in danbooru_tools._running_threads
    danbooru_tools._running_tasks.pop(identity,None)
    public = danbooru_tools.tool_task_snapshot(identity)
    assert public['status'] == 'error' and public['stage'] == 'Interrupted'
    assert danbooru_state.snapshot(identity)['status'] == 'interrupted'
    with danbooru_state.database() as db:
        row=operations.rows(db,"SELECT * FROM kv_operations WHERE owner='danbooru'")[0]
        assert row['state']=='interrupted' and row['ended_at']

def test_danbooru_ingest_writes_survive_derived_index_replacement(tmp_path):
    from modules.danbooru.pipeline import build_parser,run_import_discover
    root=tmp_path/'media';root.mkdir();(root/'sample.jpg').write_bytes(b'generated discovery fixture')
    data=tmp_path/'index.sqlite';user=tmp_path/'user.sqlite';jobs=tmp_path/'jobs.sqlite'
    sqlite3.connect(user).close()
    parser=build_parser()
    args=parser.parse_args(['--root',str(root),'--user-db',str(user),'--jobs-db',str(jobs),'import-discover','--output',str(data),str(root)])
    assert run_import_discover(args)==0
    with closing(sqlite3.connect(data)) as db,closing(sqlite3.connect(jobs)) as state:
        assert db.execute('SELECT count(*) FROM ingest_state').fetchone()==(0,)
        assert state.execute('SELECT phase,status FROM ingest_state').fetchall()==[('discovered','done')]
    data.rename(tmp_path/'retained-index.sqlite')
    assert run_import_discover(args)==0
    with closing(sqlite3.connect(jobs)) as state:
        assert state.execute('SELECT count(*) FROM ingest_state').fetchone()==(1,)


from tests.test_automatic_backups import backup_home


def seed_backup_operations(path):
    from services.job_store_migration import initialize
    path.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        initialize(db)
        operations.request(db,'fixture','saved',kind='download',key='saved',payload={})
        operations.claim(db,'fixture','saved')
        operations.transition(db,'fixture','saved',state='completed')


def test_backup_restore_keeps_user_and_operational_generation_together(backup_home,tmp_path,monkeypatch):
    import backup_bundle,config,zipfile
    home,_,_=backup_home
    monkeypatch.setattr(backup_bundle,'RECOVERY_DIR',home/'recovery')
    seed_backup_operations(config.JOBS_DB_PATH)
    bundle=backup_bundle.create_backup_bundle()
    with zipfile.ZipFile(bundle['path']) as archive_file:
        manifest=json.loads(archive_file.read('manifest.json'))
        assert manifest['operational_database'] is True
        assert 'databases/jobs.sqlite' in archive_file.namelist()
        assert 'operational_database' not in manifest['components']
    with closing(sqlite3.connect(config.JOBS_DB_PATH)) as db:
        before=operations.get(db,'fixture','saved')
        db.execute("UPDATE kv_operations SET phase='changed after backup'")
        db.commit()
    backup_bundle.restore_backup_bundle(Path(bundle['path']).name)
    with closing(sqlite3.connect(config.JOBS_DB_PATH)) as db:
        assert operations.get(db,'fixture','saved')==before
    with closing(sqlite3.connect(home/'user.sqlite')) as db:
        assert db.execute('SELECT value FROM marker').fetchone()==('preserve me',)


def test_older_backup_preserves_newer_jobs_in_recovery_before_fresh_generation(backup_home,monkeypatch):
    import backup_bundle,config
    home,_,_=backup_home
    monkeypatch.setattr(backup_bundle,'RECOVERY_DIR',home/'recovery')
    bundle=backup_bundle.create_backup_bundle()
    seed_backup_operations(config.JOBS_DB_PATH)
    result=backup_bundle.restore_backup_bundle(Path(bundle['path']).name)
    with closing(sqlite3.connect(config.JOBS_DB_PATH)) as db:
        assert operations.get(db,'fixture','saved') is None
    with closing(sqlite3.connect(Path(result['rollback_path'])/'operational_database')) as retained:
        assert operations.get(retained,'fixture','saved')['state']=='completed'


def test_backup_refuses_unknown_operations_without_rewriting_evidence(backup_home):
    import backup_bundle,config
    seed_backup_operations(config.JOBS_DB_PATH)
    with closing(sqlite3.connect(config.JOBS_DB_PATH)) as db:
        db.execute('UPDATE kv_operation_schema SET version=999');db.commit()
    with pytest.raises(ValueError,match='Unsupported operation'):
        backup_bundle.create_backup_bundle()
    with closing(sqlite3.connect(config.JOBS_DB_PATH)) as db:
        assert db.execute('SELECT version FROM kv_operation_schema').fetchone()==(999,)


def test_fresh_owner_startup_does_not_require_or_create_registered_folders(tmp_path,monkeypatch):
    from modules.youtube import _publish
    from modules.twitter import publish
    import config
    path=tmp_path/'user.sqlite'
    @contextmanager
    def user():
        with closing(sqlite3.connect(path)) as db:
            db.row_factory=lambda cursor,row:dict(zip([col[0] for col in cursor.description],row))
            yield db
    with user() as db:_publish(db);publish(db)
    monkeypatch.setattr(youtube_jobs,'get_user_db',user)
    monkeypatch.setattr(twitter_jobs,'get_user_db',user)
    monkeypatch.setattr(config,'JOBS_DB_PATH',tmp_path/'state/jobs.sqlite')
    youtube_jobs.recover();twitter_jobs.recover()
    with user() as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='files_sources'").fetchone()
        assert db.execute('SELECT count(*) AS n FROM kv_libraries').fetchone()['n']==0
    with youtube_jobs.get_jobs_db() as db:
        assert db.execute('SELECT count(*) AS n FROM youtube_downloads').fetchone()['n']==0
    with twitter_jobs.get_jobs_db() as db:
        assert db.execute('SELECT count(*) AS n FROM twitter_downloads').fetchone()['n']==0


def test_saved_art_and_playback_diagnostics_survive_history_loss_and_source_move(local):
    from tests.test_youtube_metadata import image_bytes,facts
    source,media,legacy=local
    job=youtube_jobs.create('https://youtu.be/abcdefghijk',source.source_id,request_id=uuid4())
    youtube_jobs.update(job['id'],status='downloading')
    stage=media/'.keivotos/youtube'/job['id'];stage.mkdir(parents=True)
    (stage/'video.mp4').write_bytes(b'local saved video')
    (stage/'.keivotos').mkdir()
    avatar=image_bytes()
    (stage/'.keivotos/channel-avatar.png').write_bytes(avatar)
    youtube_jobs.publish(job,stage,{'path':'video.mp4','title':'Saved',
        'capture':facts(avatar='.keivotos/channel-avatar.png',avatar_error='')})
    archive_history(youtube_jobs.config.JOBS_DB_PATH)
    moved=media.with_name('moved-media');media.rename(moved)
    with legacy() as db:
        db.execute('UPDATE files_sources SET path=? WHERE source_id=?',(str(moved),source.source_id));db.commit()
    assert youtube_router.channel_avatar(job['id']).body==avatar
    result=youtube_router.playback_error(youtube_router.YouTubePlaybackFailure(id=job['id'],reason='decode'))
    assert result=={'status':'logged'}
    with youtube_jobs.get_jobs_db() as db:
        assert db.execute('SELECT count(*) AS n FROM youtube_downloads').fetchone()['n']==0

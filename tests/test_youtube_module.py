"""YouTube lifecycle with local process/engine doubles; never download remotely."""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import sys

import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from files_base import sources, index, annotations
from files_base.serving import ServeDenied
from modules.youtube import SCHEMA, _publish
from modules.youtube import jobs, engine, worker, router, metadata, layout

URL='https://www.youtube.com/watch?v=abcdefghijk'


@pytest.fixture
def local(tmp_path,monkeypatch):
    media=tmp_path/'media';media.mkdir()
    user=tmp_path/'user.sqlite'
    @contextmanager
    def connect():
        with sqlite3.connect(user) as c:
            c.row_factory=lambda cursor,row: {column[0]:value for column,value in zip(cursor.description,row)}
            yield c
    with connect() as c:
        sources.ensure_sources_schema(c);_publish(c)
        source=sources.register_source(c,media)
    monkeypatch.setattr(jobs,'get_user_db',connect)
    monkeypatch.setattr(router,'get_user_db',connect)
    monkeypatch.setattr(jobs.config,'FILES_DB_PATH',tmp_path/'files.sqlite')
    monkeypatch.setattr(jobs.config,'SUITE_HOME',tmp_path/'home')
    monkeypatch.setattr(jobs.config,'JOBS_DB_PATH',tmp_path/'home/state/jobs.sqlite')
    monkeypatch.setattr(jobs,'dependencies',lambda:{'ready':True,'missing':[],'ffmpeg':'ffmpeg'})
    jobs.recover()
    return source,media,connect


@pytest.mark.parametrize('url',['http://youtu.be/abcdefghijk','https://youtube.com.evil/watch?v=abcdefghijk','https://youtube.com/playlist?list=abcdefghijk','https://youtube.com@localhost/watch?v=abcdefghijk','file:///etc/passwd','https://youtu.be:4433/abcdefghijk','https://youtu.be/abc'])
def test_url_validation(url):
    with pytest.raises(ValueError):engine.canonical_url(url)


def test_canonical_single_video_and_closed_qualities():
    assert engine.canonical_url('https://youtu.be/abcdefghijk?t=5')==URL
    assert engine.canonical_url('https://www.youtube.com/shorts/abcdefghijk')==URL
    assert engine.format_selector('audio','128')=='bestaudio[abr<=128]'
    assert '[height<=720]' in engine.format_selector('720','best')
    with pytest.raises(ValueError):engine.format_selector('arbitrary options','best')


def test_duplicate_cancel_retry_recovery_preserves_records(local):
    source,media,connect=local
    job=jobs.create(URL,source.source_id)
    with pytest.raises(ValueError):jobs.create(URL,source.source_id)
    jobs.cancel(job['id']);assert jobs.get(job['id'])['status']=='cancelled'
    second=jobs.create(URL,source.source_id)
    jobs.recover();assert jobs.get(second['id'])['status']=='interrupted'
    assert jobs.get(job['id'])['status']=='cancelled'
    assert not list(media.iterdir())


def test_publication_and_source_url_preserve_existing_annotations(local):
    source,media,connect=local
    job=jobs.create(URL,source.source_id);jobs.update(job['id'],status='downloading')
    staging=media/'.keivotos/youtube'/job['id'];staging.mkdir(parents=True)
    (staging/'video.mp4').write_bytes(b'completed test media')
    (staging/'cover.png').write_bytes(b'fixture thumbnail')
    jobs.publish(job,staging,{'path':'video.mp4','thumbnail':'cover.png','title':'Local result'})
    saved=jobs.get(job['id']);assert saved['status']=='complete' and saved['progress']==100
    assert (media/saved['path']).read_bytes()==b'completed test media'
    with connect() as c:
        row=c.execute('SELECT url FROM files_annotation_links').fetchone();assert row['url']==URL
    with index.open_index(jobs.config.FILES_DB_PATH) as c:
        assert c.execute('SELECT count(*) FROM files_index WHERE relative_path LIKE ?',('.keivotos%',)).fetchone()[0]==0
        assert c.execute('SELECT 1 FROM files_index WHERE relative_path=?',(saved['path'],)).fetchone()


def test_cancelled_or_moved_source_cannot_publish(local,tmp_path):
    source,media,connect=local
    job=jobs.create(URL,source.source_id)
    stage=media/'.keivotos/youtube'/job['id'];stage.mkdir(parents=True);(stage/'x.mp4').write_bytes(b'keep')
    jobs.cancel(job['id'])
    with pytest.raises(ValueError):jobs.publish(job,stage,{'path':'x.mp4','title':'x'})
    assert (stage/'x.mp4').read_bytes()==b'keep'
    job=jobs.retry(job['id'])
    jobs.update(job['id'],status='downloading')
    with connect() as c:c.execute('UPDATE files_sources SET path=? WHERE source_id=?',(str(tmp_path),source.source_id));c.commit()
    with pytest.raises(ValueError):jobs.publish(job,stage,{'path':'x.mp4','title':'x'})


def test_engine_embedding_options_and_final_event(tmp_path,monkeypatch,capsys):
    import yt_dlp
    class Engine:
        def __init__(self,options):
            self.options=options
            assert options['noplaylist'] and not options['overwrites']
            assert not options['remote_components'] and not options['cachedir']
            assert options['ffmpeg_location']=='fixture'
            assert options['js_runtimes']=={}
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def extract_info(self,url,download):
            assert url==URL and download
            path=tmp_path/'video.mp4';path.write_bytes(b'finished')
            self.options['progress_hooks'][0]({'total_bytes':100,'downloaded_bytes':50,'info_dict':{'title':'Title'}})
            self.options['post_hooks'][0](str(path))
            return {'id':'abcdefghijk','title':'Title'}
    monkeypatch.setattr(yt_dlp,'YoutubeDL',Engine)
    worker.download({'directory':str(tmp_path),'url':URL,'video':'best','audio':'best','ffmpeg':'fixture'})
    events=[json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]['progress']==47.5
    assert events[-1]['event']=='complete' and events[-1]['path']=='video.mp4'


def test_actual_child_process_success_and_cancellation(local,tmp_path,monkeypatch):
    source,media,connect=local
    script=tmp_path/'app.py'
    script.write_text('''import json,sys,time
from pathlib import Path
s=json.loads(sys.stdin.readline());p=Path(s['directory'])
if s['video']=='480':
 print(json.dumps({'event':'progress','progress':10,'title':'Waiting'}),flush=True)
 time.sleep(60)
else:
 (p/'done.mp4').write_bytes(b'completed fixture')
 print(json.dumps({'event':'complete','path':'done.mp4','title':'Fixture','thumbnail':''}),flush=True)
''')
    monkeypatch.setattr(jobs.config,'CODE_ROOT',tmp_path)
    async def run():
        good=jobs.create(URL,source.source_id);jobs.update(good['id'],status='downloading')
        await jobs.execute(good)
        assert jobs.get(good['id'])['status']=='complete'
        slow=jobs.create(URL,source.source_id,'480');jobs.update(slow['id'],status='downloading')
        task=asyncio.create_task(jobs.execute(slow))
        for _ in range(100):
            if jobs.get(slow['id'])['title']=='Waiting':break
            await asyncio.sleep(.02)
        jobs.cancel(slow['id']);await asyncio.wait_for(task,5)
        assert jobs.get(slow['id'])['status']=='cancelled'
        assert not (media/'YouTube'/slow['id']).exists()
    asyncio.run(run())


def test_library_count_and_literal_search_use_named_rows(local):
    source,media,connect=local
    job=jobs.create(URL,source.source_id)
    jobs.update(job['id'],title='100% local')
    assert router.library('%',0,60)['total']==1
    assert router.library('missing',0,60)['items']==[]
    assert 'root' not in router.library('',0,60)['items'][0]


def test_cancelled_jobs_leave_records_and_partial_files_but_not_library_cards(local):
    source,media,connect=local
    completed=jobs.create(URL,source.source_id)
    jobs.update(completed['id'],status='complete',title='Fixture complete')
    cancelled=jobs.create(URL,source.source_id)
    jobs.update(cancelled['id'],title='Fixture cancelled')
    staging=media/'.keivotos/youtube'/cancelled['id']
    staging.mkdir(parents=True)
    partial=staging/'unfinished.part'
    partial.write_bytes(b'preserved partial')
    jobs.cancel(cancelled['id'])
    failed=jobs.create(URL,source.source_id)
    jobs.update(failed['id'],status='failed',title='Fixture failed')

    page=router.library('',0,1)
    assert page['total']==2
    ids={page['items'][0]['id'],router.library('',1,1)['items'][0]['id']}
    assert ids=={failed['id'],completed['id']}
    assert router.library('cancelled',0,60)=={'items':[],'total':0}
    assert jobs.get(cancelled['id'])['status']=='cancelled'
    assert partial.read_bytes()==b'preserved partial'


def test_background_failure_retains_partial_files_and_can_retry(local,monkeypatch,caplog):
    source,media,connect=local
    async def fail(job):
        folder=jobs.child_directory(media,['.keivotos','youtube',job['id']])
        (folder/'unfinished.part').write_bytes(b'preserved partial')
        raise RuntimeError('fixture merge failure')
    monkeypatch.setattr(jobs,'execute',fail)
    async def run():
        first=jobs.create(URL,source.source_id)
        task=asyncio.create_task(jobs.run_queue())
        try:
            for _ in range(100):
                if jobs.get(first['id'])['status']=='failed':break
                await asyncio.sleep(.02)
            assert jobs.get(first['id'])['status']=='failed'
            assert (media/'.keivotos/youtube'/first['id']/'unfinished.part').read_bytes()==b'preserved partial'
            assert jobs.create(URL,source.source_id)['id']!=first['id']
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(run())
    assert 'fixture merge failure' in caplog.text


def test_dependency_failure_refused_before_creating_files(local,tmp_path,monkeypatch):
    source,media,connect=local
    monkeypatch.setattr(jobs,'dependencies',lambda:{'ready':False,'missing':['fixture runtime']})
    with pytest.raises(ValueError,match='fixture runtime'):jobs.create(URL,source.source_id)
    assert not list(media.iterdir())


def test_default_and_custom_destinations_preserve_roles_and_media(local):
    source,media,connect=local
    assert jobs.preferences()['source_id']==''
    with pytest.raises(ValueError,match='Choose a YouTube folder'):jobs.create(URL)
    with connect() as db:sources.update_source(db,source.source_id,role='youtube')
    assert jobs.preferences()['path']==str(media)
    default=jobs.create(URL)
    assert default['source_id']==source.source_id
    custom=media.parent/'custom';custom.mkdir()
    original=custom/'keep.txt';original.write_text('preserved')
    saved=jobs.save_preferences('custom',str(custom))
    assert saved['path']==str(custom)
    job=jobs.create(URL)
    assert job['source_id']==saved['source_id']
    with connect() as db:assert sources.get_source(db,saved['source_id']).role=='files'
    jobs.save_preferences('default')
    assert jobs.preferences()['path']==str(media)
    jobs.save_preferences('custom')
    assert jobs.preferences()['path']==str(custom)
    assert original.read_text()=='preserved'
    forbidden=jobs.config.SUITE_HOME;forbidden.mkdir(exist_ok=True)
    with pytest.raises(ValueError):jobs.save_preferences('custom',str(forbidden))
    assert jobs.preferences()['path']==str(custom)
    with connect() as db:sources.update_source(db,saved['source_id'],visible=False)
    with pytest.raises(ValueError):jobs.create('https://youtu.be/lmnopqrstuv')


def test_engine_requires_only_ytdlp_and_existing_ffmpeg(monkeypatch):
    checked=[]
    def find(name):
        checked.append(name)
        return object() if name=='yt_dlp' else None
    monkeypatch.setattr(engine.importlib.util,'find_spec',find)
    monkeypatch.setattr(engine.sys,'frozen',False,raising=False)
    monkeypatch.setattr(engine.shutil,'which',lambda name: '/existing/ffmpeg' if name=='ffmpeg' else pytest.fail('Unexpected runtime lookup: '+name))
    assert engine.dependencies()=={'ready':True,'missing':[],'ffmpeg':'/existing/ffmpeg'}
    assert checked==['yt_dlp']


def test_playback_diagnostics_only_accept_completed_available_jobs(local,caplog):
    from fastapi import HTTPException
    from pydantic import ValidationError
    source,media,connect=local
    job=jobs.create(URL,source.source_id)
    payload=router.YouTubePlaybackFailure(id=job['id'],reason='decode')
    with pytest.raises(HTTPException):router.playback_error(payload)
    jobs.update(job['id'],status='complete',path='YouTube/fixture.mp4')
    assert router.playback_error(payload)=={'status':'logged'}
    assert 'Playback failed' in caplog.text and job['id'] in caplog.text
    with pytest.raises(ValidationError):router.YouTubePlaybackFailure(id=job['id'],reason='arbitrary log text')
    with connect() as db:sources.update_source(db,source.source_id,visible=False)
    with pytest.raises(HTTPException):router.playback_error(payload)
    with pytest.raises(HTTPException):router.playback_error(router.YouTubePlaybackFailure(id='missing',reason='network'))


def test_retry_replaces_failed_card_and_repeated_click_returns_same_attempt(local):
    source,media,connect=local
    first=jobs.create(URL,source.source_id)
    jobs.update(first['id'],status='failed',title='Retry fixture')
    replacement=router.retry(first['id'])
    again=router.retry(first['id'])
    assert again['id']==replacement['id']
    assert router.library('Retry fixture',0,60)['total']==1
    assert [row['id'] for row in router.library('',0,60)['items']]==[replacement['id']]
    assert jobs.get(first['id'])['status']=='failed'


def test_registration_failure_keeps_published_path_for_local_retry(local,monkeypatch):
    source,media,connect=local
    first=jobs.create(URL,source.source_id)
    jobs.update(first['id'],status='downloading')
    stage=media/'.keivotos/youtube'/first['id'];stage.mkdir(parents=True)
    (stage/'video.mp4').write_bytes(b'preserved completed output')
    with monkeypatch.context() as patch:
        patch.setattr(jobs.index,'scan_source',lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('fixture index failure')))
        with pytest.raises(RuntimeError,match='fixture index failure'):
            jobs.publish(first,stage,{'path':'video.mp4','title':'Published fixture'})
    saved=jobs.get(first['id'])
    assert saved['path']==f"YouTube/{first['id']}/video.mp4"
    assert saved['failure_phase']=='integration'
    assert (media/saved['path']).read_bytes()==b'preserved completed output'


def test_additive_retry_migration_preserves_legacy_records_and_is_repeatable(tmp_path):
    original=SCHEMA.replace(" retry_of TEXT NOT NULL DEFAULT '', superseded_by TEXT NOT NULL DEFAULT '',\n failure_phase TEXT NOT NULL DEFAULT '',\n",'')
    path=tmp_path/'legacy.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript(original)
        db.execute("INSERT INTO youtube_downloads(id,url,source_id,root,video,audio,title,status,path) VALUES('old',?,'source','root','best','best','Saved','failed','YouTube/old/video.mp4')",(URL,))
        before=db.execute('SELECT * FROM youtube_downloads').fetchone()
        _publish(db);_publish(db)
        names=[column[0] for column in db.execute('SELECT * FROM youtube_downloads').description]
        after=dict(zip(names,db.execute('SELECT * FROM youtube_downloads').fetchone()))
        assert tuple(after[name] for name in names if name not in ('retry_of','superseded_by','failure_phase',*metadata.COLUMNS,*layout.COLUMNS))==before
        assert all(after[name]=='' for name in ('retry_of','superseded_by','failure_phase'))


def test_concurrent_retries_search_pagination_and_restart_keep_one_current_attempt(local):
    from concurrent.futures import ThreadPoolExecutor
    source,media,connect=local
    first=jobs.create(URL,source.source_id);jobs.update(first['id'],status='failed',title='100% retry fixture')
    stage=media/'.keivotos/youtube'/first['id'];stage.mkdir(parents=True)
    partial=stage/'unfinished.part';partial.write_bytes(b'preserved')
    with ThreadPoolExecutor(max_workers=6) as pool:
        replacements=list(pool.map(lambda _:router.retry(first['id']),range(12)))
    second=replacements[0]
    assert {row['id'] for row in replacements}=={second['id']}
    assert second['retry_of']==first['id'] and jobs.get(first['id'])['superseded_by']==second['id']
    jobs.recover()  # Reopen fresh DB connections; never automatically resume.
    assert jobs.get(second['id'])['status']=='interrupted'
    third=router.retry(second['id'])
    jobs.update(third['id'],status='complete')
    assert router.retry(first['id'])['id']==third['id']
    assert router.retry(second['id'])['id']==third['id']
    with jobs.get_jobs_db() as db:
        for n in range(125):
            db.execute("INSERT INTO youtube_downloads(id,url,source_id,root,video,audio,title,status) VALUES(?,?,?,?,?,?,?,'complete')",
                       (f'fixture-{n:03}',URL,source.source_id,str(media),'best','best',f'Fixture {n}'))
        db.commit()
    rows=[row for offset in (0,60,120) for row in router.library('',offset,60)['items']]
    assert len(rows)==len({row['id'] for row in rows})==126
    assert not {first['id'],second['id']} & {row['id'] for row in rows}
    assert router.library('%',0,60)['total']==1
    assert router.library('%',0,60)['items'][0]['id']==third['id']
    assert partial.read_bytes()==b'preserved'


@pytest.mark.parametrize('reason',['tools','queue','source','running'])
def test_rejected_retry_keeps_original_visible_and_unlinked(local,monkeypatch,reason):
    from fastapi import HTTPException
    source,media,connect=local
    first=jobs.create(URL,source.source_id);jobs.update(first['id'],status='failed',title='Keep me')
    if reason=='tools':monkeypatch.setattr(jobs,'dependencies',lambda:{'ready':False,'missing':['fixture']})
    elif reason=='queue':
        with jobs.get_jobs_db() as db:
            for number in range(20):
                db.execute("INSERT INTO youtube_downloads(id,url,source_id,root,video,audio) VALUES(?,?,?,?,?,?)",
                    (f'queue-{number}',URL+str(number),source.source_id,str(media),'best','best'))
            db.commit()
    elif reason=='source':
        with connect() as db:sources.update_source(db,source.source_id,visible=False)
    else:jobs.create(URL,source.source_id)
    with pytest.raises(HTTPException):router.retry(first['id'])
    assert jobs.get(first['id'])['superseded_by']==''
    assert any(row['id']==first['id'] for row in router.library('',0,60)['items'])
    assert not list(media.iterdir())


def run_until_terminal(job_id):
    async def run():
        task=asyncio.create_task(jobs.run_queue())
        try:
            for _ in range(250):
                if jobs.get(job_id)['status'] in ('complete','failed'):return jobs.get(job_id)
                await asyncio.sleep(.02)
            pytest.fail('Local job did not finish')
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
    return asyncio.run(run())


@pytest.mark.parametrize('failure',['index','annotation'])
def test_published_file_registration_retry_never_runs_download_and_preserves_notes(local,monkeypatch,failure):
    source,media,connect=local
    first=jobs.create(URL,source.source_id)
    async def publish_fixture(job):
        stage=media/'.keivotos/youtube'/job['id'];stage.mkdir(parents=True)
        (stage/'video.mp4').write_bytes(b'preserved published bytes')
        (stage/'cover.png').write_bytes(b'preserved cover')
        jobs.publish(job,stage,{'path':'video.mp4','thumbnail':'cover.png','title':'Published fixture'})
    monkeypatch.setattr(jobs,'execute',publish_fixture)
    with monkeypatch.context() as patch:
        target=jobs.index if failure=='index' else jobs.annotations
        symbol='scan_source' if failure=='index' else 'upsert_annotation'
        def fail(*args,**kwargs):raise RuntimeError('fixture registration failure')
        patch.setattr(target,symbol,fail)
        saved=run_until_terminal(first['id'])
    assert saved['status']=='failed' and saved['failure_phase']=='integration'
    assert 'Video saved' in saved['error']
    media_path=media/saved['path'];before={p.relative_to(media):p.read_bytes() for p in media.rglob('*') if p.is_file()}
    with index.open_index(jobs.config.FILES_DB_PATH) as db:
        index.scan_source(db,source.source_id,media)
        digest=jobs.hashing.ensure_index_hash(db,media_path)
    with connect() as db:
        annotations.ensure_annotations_schema(db)
        note=annotations.upsert_annotation(db,subject_kind='file',source_id=source.source_id,relative_path=saved['path'],content_hash=digest)
        db.execute('INSERT INTO files_annotation_links(annotation_id,url,label,kind,position) VALUES(?,?,?,?,?)',
                   (note.id,'https://example.org/preserved','Existing','source',0));db.commit()
    monkeypatch.setattr(jobs,'execute',lambda *_:pytest.fail('Registration retry started a downloader'))
    monkeypatch.setattr(jobs,'dependencies',lambda:pytest.fail('Registration retry requested download tools'))
    second=router.retry(first['id']);assert second['path']==saved['path']
    # Restart between acceptance and processing, then explicitly retry the interrupted repair.
    jobs.recover();third=router.retry(second['id'])
    complete=run_until_terminal(third['id'])
    assert complete['status']=='complete' and complete['failure_phase']==''
    assert complete['path']==saved['path']
    assert {p.relative_to(media):p.read_bytes() for p in media.rglob('*') if p.is_file()}==before
    assert not (media/'YouTube'/second['id']).exists() and not (media/'YouTube'/third['id']).exists()
    with connect() as db:
        links=db.execute('SELECT url FROM files_annotation_links WHERE annotation_id=? ORDER BY position',(note.id,)).fetchall()
        assert [row['url'] for row in links]==['https://example.org/preserved',URL]
    assert router.library('Published',0,60)['total']==1


def test_legacy_published_output_reuses_only_the_original_folder(local,monkeypatch):
    source,media,connect=local
    first=jobs.create(URL,source.source_id);jobs.update(first['id'],status='failed',title='Legacy fixture')
    folder=media/'YouTube'/first['id'];folder.mkdir(parents=True)
    (folder/'video.mp4').write_bytes(b'legacy completed bytes')
    (folder/'cover.webp').write_bytes(b'legacy cover')
    monkeypatch.setattr(jobs,'execute',lambda *_:pytest.fail('Legacy published output was downloaded again'))
    second=router.retry(first['id'])
    assert second['path']==f"YouTube/{first['id']}/video.mp4"
    assert run_until_terminal(second['id'])['status']=='complete'
    assert jobs.get(first['id'])['path']=='' and jobs.get(first['id'])['status']=='failed'
    assert len(list((media/'YouTube').iterdir()))==1


@pytest.mark.parametrize('hazard',['ambiguous','empty','unsupported','symlink','nested','missing','foreign','changed-root'])
def test_unsafe_published_output_refuses_retry_without_hiding_or_deleting(local,tmp_path,hazard):
    from fastapi import HTTPException
    source,media,connect=local
    first=jobs.create(URL,source.source_id);jobs.update(first['id'],status='failed',title='Keep unsafe evidence')
    folder=media/'YouTube'/first['id'];folder.mkdir(parents=True)
    path=folder/'video.mp4';path.write_bytes(b'preserved media')
    if hazard=='ambiguous':(folder/'another.mp4').write_bytes(b'also preserved')
    elif hazard=='empty':path.write_bytes(b'')
    elif hazard=='unsupported':path.rename(folder/'original.bin')
    elif hazard=='symlink':
        external=tmp_path/'external.mp4';external.write_bytes(b'preserved external')
        path.unlink();path.symlink_to(external)
    elif hazard=='nested':
        with connect() as db:sources.register_source(db,folder)
    elif hazard=='missing':jobs.update(first['id'],path=f"YouTube/{first['id']}/missing.mp4",failure_phase='integration')
    elif hazard=='foreign':jobs.update(first['id'],path='YouTube/other-job/video.mp4',failure_phase='integration')
    elif hazard=='changed-root':
        elsewhere=tmp_path/'elsewhere';elsewhere.mkdir()
        with connect() as db:db.execute('UPDATE files_sources SET path=? WHERE source_id=?',(str(elsewhere),source.source_id));db.commit()
    before={p.relative_to(media):p.read_bytes() for p in media.rglob('*') if p.is_file()}
    with pytest.raises(HTTPException):router.retry(first['id'])
    assert jobs.get(first['id'])['superseded_by']==''
    assert router.library('',0,60)['items'][0]['id']==first['id']
    assert {p.relative_to(media):p.read_bytes() for p in media.rglob('*') if p.is_file()}==before


def test_failed_retry_is_the_only_retryable_card_then_success_replaces_it(local,monkeypatch):
    source,media,connect=local
    first=jobs.create(URL,source.source_id);jobs.update(first['id'],status='failed',title='Download retry fixture')
    async def fail(job):raise RuntimeError('fixture media download failure')
    monkeypatch.setattr(jobs,'execute',fail)
    second=router.retry(first['id']);failed=run_until_terminal(second['id'])
    assert failed['status']=='failed' and failed['failure_phase']=='download'
    assert [row['id'] for row in router.library('',0,60)['items']]==[second['id']]
    async def success(job):
        stage=media/'.keivotos/youtube'/job['id'];stage.mkdir(parents=True)
        (stage/'video.mp4').write_bytes(b'completed on third attempt')
        jobs.publish(job,stage,{'path':'video.mp4','title':'Download retry fixture'})
    monkeypatch.setattr(jobs,'execute',success)
    third=router.retry(second['id']);assert run_until_terminal(third['id'])['status']=='complete'
    assert [row['id'] for row in router.library('',0,60)['items']]==[third['id']]
    assert jobs.get(first['id'])['status']==jobs.get(second['id'])['status']=='failed'


@pytest.fixture
def catalog_publisher(local):
    from contextlib import closing
    import tempfile
    from types import SimpleNamespace
    from services import item_catalog
    source, _, connect = local
    # Bundle limits are part of the existing portable contract. Keep generated
    # Windows fixture roots short, independent of pytest's report-directory name.
    with tempfile.TemporaryDirectory(prefix='k62-') as temporary:
        root = Path(temporary) / 'media'
        root.mkdir()
        with connect() as db:
            db.execute('UPDATE files_sources SET path=? WHERE source_id=?', (str(root), source.source_id))
            db.commit()
            source = sources.get_source(db, source.source_id)
        database = Path(temporary) / 'catalog.sqlite'
        with closing(sqlite3.connect(database)) as db:
            item_catalog.ensure_catalog_schema(db)
            item_catalog.ensure_library(db, namespace='files.source', legacy_key=source.source_id,
                library_id=source.source_id, kind='linked', display_name='Saved library')
        @contextmanager
        def catalog():
            with closing(sqlite3.connect(database)) as db, db:
                db.row_factory = sqlite3.Row
                yield db
        yield SimpleNamespace(source=source, root=root, connect=connect, catalog=catalog, database=database)


def publish_catalog_fixture(case, *, bundle=False, catalog_database=None):
    job = jobs.create(URL, case.source.source_id)
    jobs.update(job['id'], status='downloading')
    staging = case.root / '.keivotos/youtube' / job['id']
    staging.mkdir(parents=True)
    (staging / 'video.mp4').write_bytes(b'published catalog test bytes')
    (staging / 'cover.png').write_bytes(b'optional cover bytes')
    result = dict(path='video.mp4', thumbnail='cover.png', title='Saved')
    if bundle:
        result['capture'] = metadata.capture(dict(id='abcdefghijk', channel_id='UC' + 'a' * 22,
            channel='Channel', duration=3), URL, 'video.mp4')
    jobs.publish(job, staging, result, catalog_database=catalog_database)
    return jobs.get(job['id'])


@pytest.mark.parametrize('bundle', [False, True])
def test_publisher_explicit_catalog_commits_saved_facts_before_job_completion(catalog_publisher, monkeypatch, bundle):
    from services import data_migrations
    case = catalog_publisher
    original = jobs.update
    acknowledgements = []
    def update(identity, **values):
        if values.get('status') == 'complete':
            # A second connection proves the catalog was committed, rather than
            # merely visible inside the publisher's still-open transaction.
            with case.catalog() as db:
                record = metadata.preserved_capture(db, identity)
                assert record is not None
                assert metadata.preserved_library(db)['items'][0]['id'] == identity
                acknowledgements.append(identity)
        original(identity, **values)
    monkeypatch.setattr(jobs, 'update', update)
    saved = publish_catalog_fixture(case, bundle=bundle, catalog_database=case.catalog)
    assert saved['status'] == 'complete' and acknowledgements == [saved['id']]
    with case.catalog() as db:
        captured = metadata.preserved_capture(db, saved['id'])
        assert captured['payload']['record']['path'] == saved['path']
        assert captured['payload']['legacy_job_ids'] == [saved['id']]
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='youtube_downloads'").fetchone()
        before = data_migrations._state(db)
    contents = {str(p): p.read_bytes() for p in case.root.rglob('*') if p.is_file()}
    source, root = jobs.destination(saved['source_id'], saved['root'])
    jobs.register(saved, source, root, [], catalog_database=case.catalog)
    with case.catalog() as db: assert data_migrations._state(db) == before
    assert contents == {str(p): p.read_bytes() for p in case.root.rglob('*') if p.is_file()}
    with case.connect() as db:
        assert db.execute('SELECT count(*) AS n FROM files_annotation_links WHERE url=?', (URL,)).fetchone()['n'] == 1


@pytest.mark.parametrize('phase', ['index', 'annotation', 'catalog'])
def test_catalog_publisher_failure_retains_files_for_explicit_retry_without_acquisition(catalog_publisher, monkeypatch, phase):
    case = catalog_publisher
    if phase == 'catalog':
        with case.catalog() as db:
            db.executescript("CREATE TRIGGER interrupt_catalog BEFORE INSERT ON kv_item_captures WHEN new.namespace='youtube.library' BEGIN SELECT RAISE(ABORT,'registration interrupted'); END;")
    with monkeypatch.context() as patch:
        if phase != 'catalog':
            def fail(*args, **kwargs): raise RuntimeError('registration interrupted')
            patch.setattr(jobs.index if phase == 'index' else jobs.annotations,
                          'scan_source' if phase == 'index' else 'upsert_annotation', fail)
        with pytest.raises((RuntimeError, sqlite3.IntegrityError), match='registration interrupted'):
            publish_catalog_fixture(case, bundle=True, catalog_database=case.catalog)
    with jobs.get_jobs_db() as db: first = dict(db.execute('SELECT * FROM youtube_downloads').fetchone())
    assert first['status'] == 'downloading' and first['failure_phase'] == 'integration'
    assert (case.root / first['path']).read_bytes() == b'published catalog test bytes'
    contents = {str(p): p.read_bytes() for p in case.root.rglob('*') if p.is_file()}
    with case.catalog() as db:
        assert db.execute('SELECT count(*) AS n FROM kv_items').fetchone()['n'] == 0
        assert db.execute('SELECT count(*) AS n FROM kv_assets').fetchone()['n'] == 0
        if phase == 'catalog': db.execute('DROP TRIGGER interrupt_catalog'); db.commit()
    monkeypatch.setattr(jobs, 'dependencies', lambda: pytest.fail('Repair requested acquisition tools'))
    monkeypatch.setattr(jobs, 'execute', lambda *_: pytest.fail('Repair started a downloader'))
    jobs.recover()
    replacement = jobs.retry(first['id'])
    jobs.update(replacement['id'], status='downloading')
    jobs.resume_registration(replacement, catalog_database=case.catalog)
    saved = jobs.get(replacement['id'])
    assert saved['status'] == 'complete' and saved['path'] == first['path']
    with case.catalog() as db:
        captured = metadata.preserved_capture(db, saved['id'])
        assert captured['payload']['legacy_job_ids'] == [saved['id'], first['id']]
        assert metadata.preserved_library(db)['total'] == 1
    assert contents == {str(p): p.read_bytes() for p in case.root.rglob('*') if p.is_file()}


def test_default_publisher_does_not_select_or_write_catalog(catalog_publisher):
    case = catalog_publisher
    saved = publish_catalog_fixture(case)
    assert saved['status'] == 'complete'
    with case.catalog() as db:
        assert db.execute('SELECT count(*) AS n FROM kv_items').fetchone()['n'] == 0


def test_catalog_commit_survives_lost_job_acknowledgement_and_exact_repair(catalog_publisher, monkeypatch):
    from services import data_migrations
    case = catalog_publisher
    original = jobs.update
    def interrupted(identity, **values):
        if values.get('status') == 'complete': raise RuntimeError('job acknowledgement interrupted')
        original(identity, **values)
    with monkeypatch.context() as patch:
        patch.setattr(jobs, 'update', interrupted)
        with pytest.raises(RuntimeError, match='job acknowledgement interrupted'):
            publish_catalog_fixture(case, catalog_database=case.catalog)
    with jobs.get_jobs_db() as db: first = dict(db.execute('SELECT * FROM youtube_downloads').fetchone())
    with case.catalog() as db:
        before = data_migrations._state(db)
        assert metadata.preserved_library(db)['total'] == 1
    jobs.resume_registration(first, catalog_database=case.catalog)
    assert jobs.get(first['id'])['status'] == 'complete'
    with case.catalog() as db: assert data_migrations._state(db) == before


@pytest.mark.parametrize('change', ['hidden', 'relocated', 'nested'])
@pytest.mark.parametrize('timing', ['index', 'catalog'])
def test_catalog_publisher_rechecks_current_source_before_catalog_commit(catalog_publisher, monkeypatch, change, timing):
    from modules.youtube import user_schema
    case = catalog_publisher
    target, symbol = (jobs.hashing, 'ensure_index_hash') if timing == 'index' else (user_schema, 'register_catalog_assets')
    original = getattr(target, symbol)
    def changing_source(*args, **kwargs):
        result = original(*args, **kwargs)
        # Change authorization either before catalog selection or after its
        # potentially long asset hashing, while the catalog save is uncommitted.
        with case.connect() as db:
            if change == 'hidden': sources.update_source(db, case.source.source_id, visible=False)
            elif change == 'relocated':
                moved = case.root.parent / 'moved'; moved.mkdir()
                db.execute('UPDATE files_sources SET path=? WHERE source_id=?', (str(moved), case.source.source_id)); db.commit()
            else: sources.register_source(db, case.root / 'YouTube')
        return result
    monkeypatch.setattr(target, symbol, changing_source)
    with pytest.raises((ValueError, ServeDenied)):
        publish_catalog_fixture(case, catalog_database=case.catalog)
    with case.catalog() as db:
        assert db.execute('SELECT count(*) AS n FROM kv_items').fetchone()['n'] == 0
    assert any(path.name == 'video.mp4' for path in case.root.rglob('*'))


def lost_catalog_completion(case, monkeypatch, *, bundle=False):
    original = jobs.update
    def interrupted(identity, **values):
        if values.get('status') == 'complete': raise RuntimeError('lost completion acknowledgement')
        original(identity, **values)
    with monkeypatch.context() as patch:
        patch.setattr(jobs, 'update', interrupted)
        with pytest.raises(RuntimeError, match='lost completion acknowledgement'):
            publish_catalog_fixture(case, bundle=bundle, catalog_database=case.catalog)
    with jobs.get_jobs_db() as db: first = dict(db.execute('SELECT * FROM youtube_downloads').fetchone())
    jobs.recover()
    assert jobs.get(first['id'])['status'] == 'interrupted'
    return jobs.get(first['id'])


@pytest.mark.parametrize('bundle', [False, True])
@pytest.mark.parametrize('restart', [False, True])
def test_catalog_retry_hides_old_save_and_reuses_output_after_lost_completion(catalog_publisher, monkeypatch, bundle, restart):
    case = catalog_publisher
    first = lost_catalog_completion(case, monkeypatch, bundle=bundle)
    contents = {str(p): p.read_bytes() for p in case.root.rglob('*') if p.is_file()}
    with case.catalog() as db: original = metadata.preserved_capture(db, first['id'])
    monkeypatch.setattr(jobs, 'dependencies', lambda: pytest.fail('Retained retry requested tools'))
    monkeypatch.setattr(jobs, 'execute', lambda *_: pytest.fail('Retained retry started acquisition'))
    replacement = jobs.retry(first['id'], catalog_database=case.catalog)
    assert list(jobs._pending) == [replacement['id']]
    assert jobs.retry(first['id'], catalog_database=case.catalog)['id'] == replacement['id']
    with case.catalog() as db:
        assert metadata.preserved_library(db) == dict(items=[], total=0)
        assert metadata.preserved_capture(db, first['id']) == original
    if restart:
        jobs.recover()
        # Repeating an old request reconciles links but never starts an
        # interrupted attempt automatically; explicit Retry is still required.
        assert jobs.retry(first['id'], catalog_database=case.catalog)['status'] == 'interrupted'
        assert not jobs._pending
        replacement = jobs.retry(replacement['id'], catalog_database=case.catalog)
    jobs.update(replacement['id'], status='downloading')
    jobs.resume_registration(replacement, catalog_database=case.catalog)
    saved = jobs.get(replacement['id'])
    assert saved['status'] == 'complete' and saved['path'] == first['path']
    with case.catalog() as db:
        library = metadata.preserved_library(db)
        assert library['total'] == 1 and library['items'][0]['id'] == saved['id']
        assert metadata._preserved_file_capture(db, first['source_id'], first['path'])['payload']['record']['id'] == saved['id']
        assert metadata.preserved_capture(db, first['id']) == original
    assert contents == {str(p): p.read_bytes() for p in case.root.rglob('*') if p.is_file()}
    assert jobs.retry(first['id'], catalog_database=case.catalog)['id'] == saved['id']


def test_catalog_retry_acknowledgement_failure_keeps_acceptance_and_reconciles_once(catalog_publisher, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    case = catalog_publisher
    first = lost_catalog_completion(case, monkeypatch)
    with case.catalog() as db:
        db.executescript("CREATE TRIGGER interrupt_retirement BEFORE INSERT ON kv_item_captures WHEN new.namespace='youtube.retirement' BEGIN SELECT RAISE(ABORT,'retirement interrupted'); END;")
    with pytest.raises(ValueError, match='Retry accepted; catalog registration is incomplete') as failure:
        jobs.retry(first['id'], catalog_database=case.catalog)
    assert isinstance(failure.value.__cause__, sqlite3.IntegrityError)
    accepted = jobs.get(jobs.get(first['id'])['superseded_by'])
    assert accepted['status'] == 'queued' and accepted['retry_of'] == first['id']
    assert not jobs._pending
    with case.catalog() as db:
        assert metadata.preserved_library(db)['total'] == 1
        assert db.execute("SELECT count(*) FROM kv_item_captures WHERE namespace='youtube.retirement'").fetchone()[0] == 0
        db.execute('DROP TRIGGER interrupt_retirement'); db.commit()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: jobs.retry(first['id'], catalog_database=case.catalog), range(8)))
    assert {result['id'] for result in results} == {accepted['id']}
    assert list(jobs._pending) == [accepted['id']]
    with jobs.get_jobs_db() as db: assert db.execute('SELECT count(*) AS n FROM youtube_downloads').fetchone()['n'] == 2
    with case.catalog() as db:
        assert metadata.preserved_library(db)['total'] == 0
        assert db.execute("SELECT count(*) FROM kv_item_captures WHERE namespace='youtube.retirement'").fetchone()[0] == 1


def test_catalog_retry_queue_acknowledgement_failure_repairs_from_committed_links(catalog_publisher, monkeypatch):
    from collections import deque
    case = catalog_publisher
    first = lost_catalog_completion(case, monkeypatch)
    class InterruptedQueue(deque):
        def append(self, value): raise RuntimeError('queue acknowledgement interrupted')
    with monkeypatch.context() as patch:
        patch.setattr(jobs, '_pending', InterruptedQueue())
        with pytest.raises(RuntimeError, match='queue acknowledgement interrupted'):
            jobs.retry(first['id'], catalog_database=case.catalog)
    with case.catalog() as db: assert metadata.preserved_library(db)['total'] == 0
    replacement = jobs.retry(first['id'], catalog_database=case.catalog)
    assert replacement['status'] == 'queued' and list(jobs._pending) == [replacement['id']]
    with jobs.get_jobs_db() as db: assert db.execute('SELECT count(*) AS n FROM youtube_downloads').fetchone()['n'] == 2


@pytest.mark.parametrize('failure', ['queue', 'source'])
def test_catalog_retry_rejection_before_acceptance_keeps_saved_membership(catalog_publisher, monkeypatch, failure):
    case = catalog_publisher
    first = lost_catalog_completion(case, monkeypatch)
    if failure == 'queue':
        with jobs.get_jobs_db() as db:
            for number in range(20):
                db.execute("INSERT INTO youtube_downloads(id,url,source_id,root,video,audio) VALUES(?,?,?,?,?,?)",
                    (f'pending-{number}',URL+str(number),first['source_id'],first['root'],'best','best'))
            db.commit()
    else:
        with case.connect() as db: sources.update_source(db, case.source.source_id, visible=False)
    def forbidden_catalog(): pytest.fail('Rejected retry opened catalog storage')
    with pytest.raises(ValueError): jobs.retry(first['id'], catalog_database=forbidden_catalog)
    assert jobs.get(first['id'])['superseded_by'] == ''
    with case.catalog() as db: assert metadata.preserved_library(db)['total'] == 1
    with jobs.get_jobs_db() as db: assert db.execute('SELECT count(*) AS n FROM youtube_downloads').fetchone()['n'] == (21 if failure=='queue' else 1)


def test_default_retry_does_not_select_catalog_storage(catalog_publisher, monkeypatch):
    from services import data_migrations
    case = catalog_publisher
    first = lost_catalog_completion(case, monkeypatch)
    with case.catalog() as db: before = data_migrations._state(db)
    replacement = jobs.retry(first['id'])
    assert replacement['retry_of'] == first['id']
    with case.catalog() as db: assert data_migrations._state(db) == before


def test_catalog_retry_repair_respects_queue_capacity(catalog_publisher, monkeypatch):
    case = catalog_publisher
    first = lost_catalog_completion(case, monkeypatch)
    replacement = jobs.retry(first['id'])
    jobs._pending.clear()
    jobs._pending.extend(['already pending'] * 20)
    with pytest.raises(ValueError, match='Retry accepted; the queue is full'):
        jobs.retry(first['id'], catalog_database=case.catalog)
    assert len(jobs._pending) == 20 and replacement['id'] not in jobs._pending
    with case.catalog() as db: assert metadata.preserved_library(db)['total'] == 0
    jobs._pending.clear()
    assert jobs.retry(first['id'], catalog_database=case.catalog)['id'] == replacement['id']
    assert list(jobs._pending) == [replacement['id']]


@pytest.mark.parametrize('field', ['retry_of', 'url', 'source_id', 'root', 'video', 'audio'])
def test_catalog_retry_repair_refuses_conflicting_durable_links(catalog_publisher, monkeypatch, field):
    case = catalog_publisher
    first = lost_catalog_completion(case, monkeypatch)
    replacement = jobs.retry(first['id'])
    jobs._pending.clear()
    with jobs.get_jobs_db() as db:
        db.execute('UPDATE youtube_downloads SET ' + field + '=? WHERE id=?', ('changed', replacement['id'])); db.commit()
    with pytest.raises(ValueError, match='Retry accepted; catalog registration is incomplete'):
        jobs.retry(first['id'], catalog_database=case.catalog)
    assert not jobs._pending
    with case.catalog() as db: assert metadata.preserved_library(db)['total'] == 1


def test_catalog_registration_refuses_a_conflicting_acceptance_backlink(catalog_publisher, monkeypatch):
    from services import data_migrations
    case = catalog_publisher
    first = lost_catalog_completion(case, monkeypatch)
    replacement = jobs.retry(first['id'])
    with jobs.get_jobs_db() as db:
        db.execute('UPDATE youtube_downloads SET superseded_by=? WHERE id=?', ('foreign', first['id'])); db.commit()
    with case.catalog() as db: before = data_migrations._state(db)
    jobs.update(replacement['id'], status='downloading')
    with pytest.raises(ValueError, match='accepted retry history changed'):
        jobs.resume_registration(replacement, catalog_database=case.catalog)
    with case.catalog() as db: assert data_migrations._state(db) == before
    assert (case.root / first['path']).read_bytes() == b'published catalog test bytes'


def test_durable_queue_preserves_intake_order_with_equal_wall_clock_times(local,monkeypatch):
    source,_,_=local
    from uuid import UUID
    identifiers=iter([UUID(hex=value*32) for value in ('f','0','a')])
    monkeypatch.setattr(jobs.uuid,'uuid4',lambda:next(identifiers))
    queued=[jobs.create('https://youtu.be/'+identity,source.source_id)['id']
            for identity in ('abcdefghijk','lmnopqrstuv','zyxwvutsrqp')]
    seen=[]
    async def execute(job):
        seen.append(job['id'])
        jobs.update(job['id'],status='complete',progress=100)
    monkeypatch.setattr(jobs,'execute',execute)
    async def check():
        runner=asyncio.create_task(jobs.run_queue())
        try:
            for _ in range(100):
                if len(seen)==3:break
                await asyncio.sleep(.01)
            assert seen==queued
        finally:
            runner.cancel()
            await asyncio.gather(runner,return_exceptions=True)
    asyncio.run(check())


def test_selected_split_tracks_report_combined_bytes_and_reserve_completion():
    progress=worker.TransferProgress()
    progress.plan({'requested_formats':[{'format_id':'v','filesize':800},{'format_id':'a','filesize':200}]})
    assert progress.observe({'info_dict':{'format_id':'v'},'total_bytes':800,'downloaded_bytes':400})==40
    assert progress.observe({'info_dict':{'format_id':'v'},'total_bytes':800,'downloaded_bytes':800,'status':'finished'})==80
    assert progress.observe({'info_dict':{'format_id':'a'},'total_bytes':200,'downloaded_bytes':100})==90
    assert progress.observe({'info_dict':{'format_id':'a'},'total_bytes':200,'downloaded_bytes':200,'status':'finished'})==99


def test_unknown_track_sizes_stay_unknown_until_the_downloader_supplies_them():
    progress=worker.TransferProgress()
    progress.plan({'requested_formats':[{'format_id':'v'},{'format_id':'a'}]})
    assert progress.observe({'info_dict':{'format_id':'v'},'downloaded_bytes':50}) is None
    assert progress.observe({'info_dict':{'format_id':'v'},'downloaded_bytes':100,'status':'finished'}) is None
    assert progress.observe({'info_dict':{'format_id':'a'},'total_bytes_estimate':100,'downloaded_bytes':50})==75
    assert progress.observe({'info_dict':{'format_id':'a'},'total_bytes':100,'downloaded_bytes':50})==75


@pytest.mark.parametrize('size',[None,0,-1,True,float('nan'),float('inf')])
def test_invalid_or_unknown_totals_never_create_a_percentage(size):
    progress=worker.TransferProgress()
    assert progress.observe({'info_dict':{},'downloaded_bytes':50,'total_bytes':size}) is None


def test_real_worker_hooks_publish_title_before_bytes_and_keep_it_after_merge(tmp_path,monkeypatch,capsys):
    import yt_dlp
    title='Original YouTube 한글 title'
    class Engine:
        def __init__(self,options):self.options=options
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def extract_info(self,url,download):
            match=self.options['match_filter']
            assert match({'id':'abcdefghijk','title':title},incomplete=True) is None
            assert match({'id':'abcdefghijk','title':title},incomplete=False) is None
            assert match({'id':'abcdefghijk','title':title,'format_id':'v+a',
                'requested_formats':[{'format_id':'v','filesize':800},{'format_id':'a','filesize':200}]},incomplete=False) is None
            hook=self.options['progress_hooks'][0]
            hook({'info_dict':{'format_id':'v','title':title},'filename':str(tmp_path/'video.fv.webm'),
                  'tmpfilename':str(tmp_path/'video.fv.webm.part'),'total_bytes':800,'downloaded_bytes':400})
            path=tmp_path/'video.webm';path.write_bytes(b'local synthetic output')
            self.options['post_hooks'][0](str(path))
            return {'id':'abcdefghijk','title':title}
    monkeypatch.setattr(yt_dlp,'YoutubeDL',Engine)
    worker.download({'directory':str(tmp_path),'url':URL,'video':'best','audio':'best','ffmpeg':'fixture'})
    events=[json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]=={'event':'metadata','title':title}
    rows=[event for event in events if event['event']=='file']
    assert rows[0]['progress']==40 and all(row['name']==title for row in rows)
    assert events[-1]['event']=='complete' and events[-1]['title']==title and events[-1]['path']=='video.webm'


def _random_saved_video(local, number, title='Random candidate'):
    source, media, _ = local
    job = jobs.create('https://youtu.be/'+f'rand{number:07d}', source.source_id)
    jobs.update(job['id'], status='downloading')
    staging = media / '.keivotos/youtube' / job['id']; staging.mkdir(parents=True)
    (staging/'video.mp4').write_bytes(('generated random video '+str(number)).encode())
    jobs.publish(job, staging, {'path':'video.mp4','title':title})
    return jobs.get(job['id'])


def _youtube_random(current='', q=''):
    from fastapi import Response
    response = Response(); result = router.random_video(response, q, current)
    assert response.headers['cache-control'] == 'no-store'
    return result


def test_random_youtube_current_exclusion_literal_search_and_preservation(local, monkeypatch):
    monkeypatch.setattr(router, 'require_enabled', lambda _: None)
    first = _random_saved_video(local, 1, 'École 100%_ first')
    second = _random_saved_video(local, 2, 'École 100%_ second')
    original = {p.relative_to(local[1]).as_posix():p.read_bytes() for p in local[1].rglob('*') if p.is_file()}
    assert _youtube_random(first['id'], 'éCOLE 100%_')['item']['id'] == second['id']
    assert _youtube_random(second['id'], 'second')['item'] is None
    assert _youtube_random('', 'absent literal')['item'] is None
    assert original == {p.relative_to(local[1]).as_posix():p.read_bytes() for p in local[1].rglob('*') if p.is_file()}
    assert 'root' not in _youtube_random(first['id'])['item']


def test_random_youtube_reaches_later_pages_without_active_downloads(local, monkeypatch):
    monkeypatch.setattr(router, 'require_enabled', lambda _: None)
    target = _random_saved_video(local, 1)
    current = _random_saved_video(local, 2)
    for number in range(3, 126):
        placeholder = jobs.create('https://youtu.be/'+f'rand{number:07d}', local[0].source_id)
        jobs.update(placeholder['id'], status='failed')
    with jobs.get_jobs_db() as connection:
        connection.execute("UPDATE youtube_downloads SET created_at='2000-01-01T00:00:00Z' WHERE id=?", (target['id'],)); connection.commit()
    monkeypatch.setattr(router.random, 'sample', lambda population, count: list(reversed(population)))
    result = _youtube_random(current['id'])
    assert result['item']['id'] == target['id'] and result['offset'] >= 120 and result['total'] == 125


@pytest.mark.parametrize('state', ['queued','failed','audio','unsupported','retired','missing','directory','hidden','child','private','symlink'])
def test_random_youtube_skips_ineligible_and_unavailable(local, monkeypatch, tmp_path, state):
    monkeypatch.setattr(router, 'require_enabled', lambda _: None)
    current = _random_saved_video(local, 1)
    candidate = _random_saved_video(local, 2)
    source, media, connect = local; path = media/candidate['path']
    if state in ('queued','failed','audio','unsupported','retired'):
        fields = {'queued': {'status':'queued'}, 'failed': {'status':'failed'}, 'audio': {'video':'audio'},
                  'unsupported': {'path':'unsupported.mkv'}, 'retired': {'superseded_by':current['id']}}[state]
        with jobs.get_jobs_db() as connection:
            connection.execute('UPDATE youtube_downloads SET '+','.join(name+'=?' for name in fields)+' WHERE id=?', (*fields.values(),candidate['id'])); connection.commit()
    elif state in ('missing','directory','symlink'):
        path.rename(path.with_suffix('.preserved-fixture'))
        if state == 'directory': path.mkdir()
        elif state == 'symlink':
            outside = tmp_path/'outside.mp4'; outside.write_bytes(b'outside fixture')
            try: path.symlink_to(outside)
            except OSError: pytest.skip('Creating a native symlink requires OS permission')
    elif state == 'hidden':
        with connect() as connection: sources.update_source(connection, source.source_id, visible=False)
    elif state == 'child':
        with connect() as connection: sources.register_source(connection, path.parent)
    elif state == 'private': monkeypatch.setattr(router.config, 'SUITE_HOME', path.parent)
    assert _youtube_random(current['id'])['item'] is None
    assert (media/current['path']).read_bytes() == b'generated random video 1'


def test_random_youtube_changed_snapshot_and_disabled_owner(local, monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(router, 'require_enabled', lambda _: None)
    first = _random_saved_video(local, 1); _random_saved_video(local, 2)
    original = router.library; calls=[]
    def changed(*args):
        result=original(*args); calls.append(args)
        if len(calls)>1: result['total']+=1
        return result
    monkeypatch.setattr(router,'library',changed)
    monkeypatch.setattr(router.random,'sample',lambda population,count:list(reversed(population)))
    with pytest.raises(HTTPException) as error: _youtube_random(first['id'])
    assert error.value.status_code==409
    def disabled(_): raise RuntimeError('Module is disabled')
    monkeypatch.setattr(router,'require_enabled',disabled)
    with pytest.raises(HTTPException) as error: _youtube_random(first['id'])
    assert error.value.status_code==409


def test_random_youtube_api_validates_new_parameters(local, monkeypatch):
    import asyncio
    from urllib.parse import urlencode
    from fastapi import FastAPI
    monkeypatch.setattr(router,'require_enabled',lambda _:None)
    app = FastAPI(); app.include_router(router.router)
    async def request(params):
        messages = []
        async def receive(): return {'type':'http.request','body':b'','more_body':False}
        async def send(message): messages.append(message)
        await app({'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'GET',
                   'scheme':'http','path':'/api/youtube/random','raw_path':b'/api/youtube/random',
                   'query_string':urlencode(params).encode(),'root_path':'','headers':[],
                   'server':('fixture',80),'client':('127.0.0.1',1)},receive,send)
        return next(message['status'] for message in messages if message['type']=='http.response.start')
    async def verify():
        for params in ({'q':'x'*257}, {'current_id':'x'*65}): assert await request(params) == 422
    asyncio.run(verify())

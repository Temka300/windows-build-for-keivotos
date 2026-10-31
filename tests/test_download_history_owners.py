"""Real owner boundaries, local engine doubles and retained receipt provenance."""
from contextlib import contextmanager
from dataclasses import replace
import io
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from services import download_history as history, operations, item_catalog, item_manifest
from module_registry import build_registry
from modules.youtube import downloads as youtube_history
from modules.twitter import downloads as twitter_history
from modules.danbooru import downloads as danbooru_history
from tests.test_youtube_module import local
from tests.test_twitter_module import catalog


def read(jobs,user,index,home):
    return history.page(jobs_path=jobs,user_path=user,registry=build_registry(home,'test'))['items']


def capture(db,owner,item_id,namespace,payload,key='receipt'):
    item_catalog.ensure_catalog_schema(db)
    item_catalog.ensure_item(db,namespace='test.'+owner,legacy_key=item_id,owner=owner,item_type='media',item_id=item_id)
    item_catalog.record_capture(db,item_id=item_id,namespace=namespace,capture_key=key,format_version=1,payload=payload)


def active(db,owner,job='job'):
    operations.ensure_schema(db)
    operations.request(db,owner,job,kind='download',key=job,payload={})
    return operations.claim(db,owner,job)


def test_youtube_split_tracks_form_one_final_row_and_repair_keeps_its_date(local,tmp_path,monkeypatch):
    from modules.youtube import jobs
    source,media,connect=local
    job=jobs.create('https://youtu.be/abcdefghijk',source.source_id)
    jobs.update(job['id'],status='downloading')
    with jobs.get_jobs_db() as db:
        youtube_history.event(db,job['id'],{'event':'file','name':'Saved video','progress':None})
        youtube_history.event(db,job['id'],{'event':'file','name':'Saved video','progress':None})
    stage=media/'.keivotos/youtube'/job['id'];stage.mkdir(parents=True)
    (stage/'video.mp4').write_bytes(b'completed local test media')
    original=jobs.register
    monkeypatch.setattr(jobs,'register',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('registration fixture')))
    with pytest.raises(RuntimeError):
        jobs.publish(job,stage,{'path':'video.mp4','title':'Saved video','thumbnail':''})
    before=read(jobs.config.JOBS_DB_PATH,tmp_path/'user.sqlite',jobs.config.FILES_DB_PATH,tmp_path)
    assert len(before)==1 and before[0]['name']=='Saved video' and before[0]['progress']==100
    jobs.update(job['id'],status='failed',failure_phase='integration')
    replacement=jobs.retry(job['id']);jobs.update(replacement['id'],status='downloading')
    monkeypatch.setattr(jobs,'register',original)
    jobs.resume_registration(jobs.get(replacement['id']))
    after=read(jobs.config.JOBS_DB_PATH,tmp_path/'user.sqlite',jobs.config.FILES_DB_PATH,tmp_path)
    assert after==before and (media/jobs.get(replacement['id'])['path']).read_bytes()==b'completed local test media'


def test_youtube_legacy_completion_does_not_use_job_creation_or_migration_date(tmp_path):
    user=tmp_path/'user.sqlite';jobs=tmp_path/'jobs.sqlite'
    with sqlite3.connect(user) as db:
        capture(db,'youtube','item','youtube.download',{'legacy_job_ids':['old'],
            'record':{'id':'old','path':'YouTube/video.mp4','captured_at':'','created_at':'2030-01-01T00:00:00Z'}} ,key='old')
    row=read(jobs,user,tmp_path/'index.sqlite',tmp_path)[0]
    assert row['date_at'] is None and row['date_source']=='unknown' and row['progress']==100
    with sqlite3.connect(user) as db:
        capture(db,'youtube','second','youtube.download',{'legacy_job_ids':['second'],
            'record':{'id':'second','path':'YouTube/audio.m4a','captured_at':'2027-01-16T00:10:00+09:00'}},key='second')
    rows=read(jobs,user,tmp_path/'index.sqlite',tmp_path)
    assert rows[0]['date_at']=='2027-01-15T15:10:00.000000Z'
    assert rows[1]['date_at'] is None


def test_danbooru_legacy_only_uses_explicit_saved_download_facts(tmp_path):
    user=tmp_path/'user.sqlite'
    with sqlite3.connect(user) as db:
        for index,origin in enumerate([{'downloaded_at':'2027-01-16T00:00:00+09:00'},{},{'downloaded_at':'naive unknown'}]):
            identity=str(uuid4())
            value=item_manifest.new(item_id=identity,owner='danbooru',item_type='post-image',
                library_id=str(uuid4()),library_name='Images',
                assets=[item_manifest.asset(str(index)+'.png',role='media',size=1,sha256='0'*64)],origin=origin)
            capture(db,'danbooru',identity,'portable.manifest',value,key=str(index))
        db.execute('CREATE TABLE files(id INTEGER,downloaded_at TEXT)')
        db.execute("INSERT INTO files VALUES(99,'2030-01-01T00:00:00Z')");db.commit()
    rows=read(tmp_path/'jobs.sqlite',user,tmp_path/'index.sqlite',tmp_path)
    assert len(rows)==2
    assert rows[0]['date_at']=='2027-01-15T15:00:00.000000Z'
    assert rows[1]['date_at'] is None


def test_twitter_legacy_multi_media_not_avatar_and_dates_remain_unknown(tmp_path):
    user=tmp_path/'user.sqlite'
    with sqlite3.connect(user) as db:
        capture(db,'twitter','tweet','twitter.asset-observation',{'files':{
            'photo-1.jpg':{},'photo-2.png':{},'avatar.png':{},'tweet.json':{},'source.json':{}}})
    rows=read(tmp_path/'jobs.sqlite',user,tmp_path/'index.sqlite',tmp_path)
    assert {r['name'] for r in rows}=={'photo-1.jpg','photo-2.png'}
    assert all(r['date_at'] is None and r['date_source']=='unknown' for r in rows)


def test_twitter_partial_retry_and_extension_change_do_not_duplicate_media(tmp_path):
    jobs=tmp_path/'jobs.sqlite'
    with sqlite3.connect(jobs) as db:
        token=active(db,'twitter')
        with operations.bind('twitter','job',token):
            twitter_history.event(db,'job',{'event':'file','name':'photo-1.jpg','progress':35})
            twitter_history.event(db,'job',{'event':'file','name':'photo-2.jpg','progress':15})
            twitter_history.complete(db,'job',{'photo-1.jpg':{},'avatar.png':{}})
            operations.transition(db,'twitter','job',state='interrupted')
        operations.retry(db,'twitter','job','retry-token')
        new=operations.claim(db,'twitter','job')
        with operations.bind('twitter','job',new):
            twitter_history.event(db,'job',{'event':'file','name':'photo-2.webp','progress':40})
            twitter_history.complete(db,'job',{'photo-2.webp':{}})
            operations.transition(db,'twitter','job',state='completed')
    rows=read(jobs,tmp_path/'user.sqlite',tmp_path/'index.sqlite',tmp_path)
    assert len(rows)==2 and {r['name'] for r in rows}=={'photo-1.jpg','photo-2.webp'}
    assert all(r['progress']==100 for r in rows)


@pytest.mark.parametrize('name',['avatar.png','../photo-1.jpg','photo-5.jpg','photo-1.exe'])
def test_twitter_worker_file_protocol_rejects_non_media(tmp_path,name):
    with sqlite3.connect(':memory:') as db:
        active(db,'twitter')
        with pytest.raises(ValueError):twitter_history.event(db,'job',{'event':'file','name':name,'progress':30})


def test_gallery_protocol_measured_progress_and_safe_unicode_names(tmp_path):
    import gallery_dl.output
    output=[]
    media=tmp_path/"雪 quote'.png";media.write_bytes(b'verified bytes')
    jobs=tmp_path/'jobs.sqlite'
    with sqlite3.connect(jobs) as db:token=active(db,'danbooru')
    tracker=danbooru_history.Transfer({'jobs':str(jobs),'id':'job','token':token},tmp_path)
    with patch.object(gallery_dl.output.config,'get',return_value=False),patch.object(gallery_dl.output,'stdout_write',output.append),patch.object(gallery_dl.output,'stdout_write_flush',output.append),patch.object(gallery_dl.output,'stderr_write',output.append):
        engine=gallery_dl.output.CustomOutput(danbooru_history.OUTPUT)
        engine.start(str(media));engine.progress(1000,350,100)
        for line in output:assert tracker.output(line)
        row=read(jobs,tmp_path/'user.sqlite',tmp_path/'index.sqlite',tmp_path)[0]
        assert row['progress']==35 and row['name']==media.name
        engine.success(str(media))
        tracker.output(output[-1])
    row=read(jobs,tmp_path/'user.sqlite',tmp_path/'index.sqlite',tmp_path)[0]
    assert row['progress']==100 and row['completed_at']
    count=len(read(jobs,tmp_path/'user.sqlite',tmp_path/'index.sqlite',tmp_path))
    tracker.output(danbooru_history.PREFIX+'SKIP:'+repr(str(media))+'\n')
    assert len(read(jobs,tmp_path/'user.sqlite',tmp_path/'index.sqlite',tmp_path))==count
    from files_base.serving import ServeDenied
    with pytest.raises(ServeDenied):tracker.output(danbooru_history.PREFIX+'SUCCESS:'+repr(str(tmp_path/'missing.png'))+'\n')


def test_gallery_explicit_standalone_context_and_dry_unconfigured_context(tmp_path):
    from services.job_store_migration import initialize
    jobs=tmp_path/'jobs.sqlite'
    with sqlite3.connect(jobs) as db:initialize(db)
    args=SimpleNamespace(jobs_db=jobs)
    with danbooru_history.transfer(args,tmp_path) as tracker:
        media=tmp_path/'sample.jpg';media.write_bytes(b'output')
        tracker.output(danbooru_history.PREFIX+'START:'+repr(str(media))+'\n')
        tracker.output(danbooru_history.PREFIX+'SUCCESS:'+repr(str(media))+'\n')
        tracker.code=0
    assert len(read(jobs,tmp_path/'user.sqlite',tmp_path/'index.sqlite',tmp_path))==1
    before=jobs.read_bytes()
    with danbooru_history.transfer(SimpleNamespace(jobs_db=None),tmp_path) as tracker:
        assert tracker.operation is None
    assert jobs.read_bytes()==before


def test_changed_capture_receipt_refuses_without_rewriting_source(tmp_path):
    user=tmp_path/'user.sqlite'
    with sqlite3.connect(user) as db:
        capture(db,'youtube','one','youtube.download',{'legacy_job_ids':['one'],'record':{'path':'a.mp4','captured_at':''}})
        db.execute("UPDATE kv_item_captures SET payload_json='{}'");db.commit()
    before=user.read_bytes()
    with pytest.raises(sqlite3.OperationalError):
        read(tmp_path/'jobs.sqlite',user,tmp_path/'index.sqlite',tmp_path)
    assert user.read_bytes()==before


def test_twitter_saved_capture_time_is_used_without_reading_media(tmp_path):
    user=tmp_path/'user.sqlite'
    with sqlite3.connect(user) as db:
        identity=str(uuid4())
        capture(db,'twitter',identity,'twitter.asset-observation',{'files':{'video.mp4':{}}})
        value=item_manifest.new(item_id=identity,owner='twitter',item_type='saved-post',
            library_id=str(uuid4()),library_name='Twitter',
            assets=[item_manifest.asset('video.mp4',role='video',size=1,sha256='0'*64)],
            origin={'captured_at':'2027-01-16T00:00:01+09:00'})
        capture(db,'twitter',identity,'portable.manifest',value)
    row=read(tmp_path/'jobs.sqlite',user,tmp_path/'index.sqlite',tmp_path)[0]
    assert row['date_at']=='2027-01-15T15:00:01.000000Z'
    assert row['date_source']=='recorded-manifest'
    assert row['started_at'] is None and row['completed_at'] is None


def test_gallery_final_extension_change_reconciles_the_started_file(tmp_path):
    path=tmp_path/'jobs.sqlite'
    with sqlite3.connect(path) as db:token=active(db,'danbooru')
    tracker=danbooru_history.Transfer({'jobs':str(path),'id':'job','token':token},tmp_path)
    before=tmp_path/'image.part';after=tmp_path/'image.jpg'
    tracker.output(danbooru_history.PREFIX+'START:'+repr(str(before))+'\n')
    tracker.output(danbooru_history.PREFIX+'PROGRESS:25\n')
    after.write_bytes(b'completed')
    tracker.output(danbooru_history.PREFIX+'SUCCESS:'+repr(str(after))+'\n')
    rows=read(path,tmp_path/'user.sqlite',tmp_path/'index.sqlite',tmp_path)
    assert len(rows)==1 and rows[0]['name']=='image.jpg' and rows[0]['progress']==100


def test_explicit_cli_recovers_only_its_exited_original_supervisor(tmp_path):
    import os,subprocess,time
    from services.job_store_migration import initialize
    jobs=tmp_path/'jobs.sqlite';ready=tmp_path/'ready'
    with sqlite3.connect(jobs) as db:initialize(db)
    script=r"""import sys,time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
from modules.danbooru.downloads import transfer,PREFIX
root=Path(sys.argv[2])
with transfer(SimpleNamespace(jobs_db=root/'jobs.sqlite'),root) as tracker:
 tracker.output(PREFIX+'START:'+repr(str(root/'file.jpg'))+'\n')
 (root/'ready').write_text('ready')
 time.sleep(60)
"""
    child=subprocess.Popen([sys.executable,'-I','-c',script,str(Path(history.__file__).parents[1]),str(tmp_path)],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        for _ in range(200):
            if ready.exists():break
            if child.poll() is not None:raise AssertionError(child.communicate()[1].decode())
            time.sleep(.02)
        assert ready.exists()
        with sqlite3.connect(jobs) as db:
            danbooru_history.recover(db)
            assert db.execute("SELECT state FROM kv_operations").fetchone()[0]=='started'
        child.kill();child.wait(timeout=10)
        with sqlite3.connect(jobs) as db:
            danbooru_history.recover(db)
            assert db.execute("SELECT state FROM kv_operations").fetchone()[0]=='interrupted'
        assert read(jobs,tmp_path/'user.sqlite',tmp_path/'index.sqlite',tmp_path)[0]['state']=='interrupted'
    finally:
        if child.poll() is None:child.kill()
        child.communicate(timeout=10)


def test_history_store_loss_never_invalidates_saved_youtube_output(local,tmp_path):
    from modules.youtube import jobs
    source,media,connect=local
    job=jobs.create('https://youtu.be/abcdefghijk',source.source_id)
    jobs.update(job['id'],status='downloading')
    stage=media/'.keivotos/youtube'/job['id'];stage.mkdir(parents=True)
    (stage/'video.mp4').write_bytes(b'preserved completed content')
    jobs.publish(job,stage,{'path':'video.mp4','title':'Saved','thumbnail':''})
    item=jobs.get(job['id']);output=media/item['path'];before=output.read_bytes()
    path=jobs.config.JOBS_DB_PATH
    # Preserve the entire old store as evidence, simulating a missing history DB.
    path.rename(path.with_name('retained-old-jobs.sqlite'))
    rows=read(path,tmp_path/'user.sqlite',jobs.config.FILES_DB_PATH,tmp_path)
    assert rows and rows[0]['name']=='Saved'
    assert not path.exists() and output.read_bytes()==before


def test_youtube_actual_child_file_events_reach_durable_history(local,tmp_path,monkeypatch):
    import asyncio
    from modules.youtube import jobs
    source,media,connect=local
    script=tmp_path/'app.py'
    script.write_text('''import json,sys,time
from pathlib import Path
spec=json.loads(sys.stdin.readline());folder=Path(spec['directory'])
(folder/'video.mp4').write_bytes(b'local child output')
print(json.dumps({'event':'file','name':'video.mp4','progress':25}),flush=True)
for _ in range(200):
 if (folder/'continue').exists():break
 time.sleep(.05)
else:raise SystemExit(2)
print(json.dumps({'event':'complete','path':'video.mp4','title':'Local child','thumbnail':''}),flush=True)
''')
    monkeypatch.setattr(jobs.config,'CODE_ROOT',tmp_path)
    job=jobs.create('https://youtu.be/abcdefghijk',source.source_id)
    jobs.update(job['id'],status='downloading')
    async def run():
        task=asyncio.create_task(jobs.execute(job))
        try:
            for _ in range(200):
                rows=read(jobs.config.JOBS_DB_PATH,tmp_path/'user.sqlite',jobs.config.FILES_DB_PATH,tmp_path)
                if rows and rows[0]['progress']==25:break
                if task.done():await task
                await asyncio.sleep(.02)
            assert rows and rows[0]['progress']==25
            (media/'.keivotos/youtube'/job['id']/'continue').write_text('continue')
            await asyncio.wait_for(task,15)
        finally:
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run())
    rows=read(jobs.config.JOBS_DB_PATH,tmp_path/'user.sqlite',jobs.config.FILES_DB_PATH,tmp_path)
    assert len(rows)==1 and rows[0]['state']=='completed' and rows[0]['progress']==100
    assert rows[0]['started_at'] and rows[0]['completed_at']>=rows[0]['started_at']


def test_twitter_actual_child_file_events_survive_failure(catalog,tmp_path,monkeypatch):
    import asyncio
    from modules.twitter import jobs
    from tests.test_twitter_module import new_job
    script=tmp_path/'app.py'
    script.write_text('''import json,sys
spec=json.loads(sys.stdin.readline())
print(json.dumps({'event':'file','name':'photo-1.jpg','progress':25}),flush=True)
print(json.dumps({'event':'failure','error':'Synthetic transport failure'}),flush=True)
raise SystemExit(1)
''')
    monkeypatch.setattr(jobs.config,'CODE_ROOT',tmp_path)
    job=new_job(catalog);jobs.update(job['id'],status='running')
    with pytest.raises(ValueError,match='Synthetic transport failure'):
        asyncio.run(jobs.execute(job))
    jobs.update(job['id'],status='failed')
    rows=read(jobs.config.JOBS_DB_PATH,jobs.config.USER_DB_PATH,jobs.config.FILES_DB_PATH,tmp_path)
    assert len(rows)==1 and rows[0]['state']=='failed' and rows[0]['progress']==25
    assert rows[0]['completed_at'] is None


def test_twitter_active_delete_stops_capture_and_preserves_other_photo_and_metadata(catalog,tmp_path,monkeypatch):
    import asyncio
    from modules.twitter import jobs
    from services import download_controls as controls
    from tests.test_twitter_module import new_job
    script=tmp_path/'app.py'
    script.write_text('''import json,sys,time
from pathlib import Path
spec=json.loads(sys.stdin.readline());folder=(Path(spec['root'])/spec['path']).parent
(folder/'photo-2.jpg').write_bytes(b'preserved sibling photo')
(folder/'metadata.json').write_bytes(b'preserved metadata')
while True:
 with (folder/'photo-1.jpg').open('ab') as out:out.write(b'x')
 print(json.dumps({'event':'file','name':'photo-1.jpg','progress':25}),flush=True)
 time.sleep(.05)
''')
    monkeypatch.setattr(jobs.config,'CODE_ROOT',tmp_path)
    job=new_job(catalog);jobs.update(job['id'],status='running')
    async def run():
        task=asyncio.create_task(jobs.execute(job))
        try:
            for _ in range(200):
                rows=read(jobs.config.JOBS_DB_PATH,jobs.config.USER_DB_PATH,jobs.config.FILES_DB_PATH,tmp_path)
                if rows and rows[0]['can_delete']:break
                if task.done():await task
                await asyncio.sleep(.02)
            assert rows and rows[0]['can_delete']
            folder=(catalog[1]/jobs.get(job['id'])['path']).parent
            row=rows[0]
            result=await asyncio.to_thread(controls.delete_active,item_id=row['id'],expected_version=row['control_version'],
                request_id=uuid4(),confirm=False,jobs_path=jobs.config.JOBS_DB_PATH,user_path=jobs.config.USER_DB_PATH,
                home=jobs.config.SUITE_HOME,registry=build_registry(jobs.config.SUITE_HOME,'test'))
            assert result['deleted'] and not (folder/'photo-1.jpg').exists()
            assert (folder/'photo-2.jpg').read_bytes()==b'preserved sibling photo'
            assert (folder/'metadata.json').read_bytes()==b'preserved metadata'
            assert await asyncio.wait_for(task,15) is False
            assert jobs.get(job['id'])['status']=='cancelled'
        finally:
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run())


def test_explicit_gallery_child_delete_uses_only_its_media_target(tmp_path,monkeypatch):
    import os,subprocess,time
    from files_base import sources
    from modules.danbooru import operational_state
    from services import download_controls as controls,process_control
    from services.job_store_migration import initialize
    media=tmp_path/'media';media.mkdir();jobs=tmp_path/'jobs.sqlite';user=tmp_path/'user.sqlite'
    with sqlite3.connect(jobs) as db:initialize(db)
    with sqlite3.connect(user) as db:
        db.row_factory=sqlite3.Row;sources.ensure_sources_schema(db)
        source=sources.register_source(db,media);sources.update_source(db,source.source_id,role='danbooru');db.commit()
    @contextmanager
    def connect():
        with sqlite3.connect(jobs) as db:yield db
    monkeypatch.setattr(operational_state,'database',connect)
    monkeypatch.delenv('KEIVOTOS_DANBOORU_OPERATION',raising=False)
    child=None
    try:
        with danbooru_history.transfer(SimpleNamespace(jobs_db=jobs,user_db=user),media) as tracker:
            command="import sys,time;from pathlib import Path;p=Path(sys.argv[1]);\\nwhile True:\\n with p.open('ab') as out:out.write(b'x')\\n time.sleep(.02)".replace('\\n','\n')
            child=subprocess.Popen([sys.executable,'-I','-c',command,str(media/'one.jpg.part')],
                **({'creationflags':0x08000000} if sys.platform=='win32' else {'start_new_session':True}))
            tracker.register(child.pid)
            for _ in range(200):
                if (media/'one.jpg.part').exists():break
                time.sleep(.02)
            (media/'archive.sqlite3').write_bytes(b'preserved gallery archive')
            (media/'one.jpg.json').write_bytes(b'preserved sidecar')
            tracker.output(danbooru_history.PREFIX+'START:'+repr(str(media/'one.jpg')))
            registry=build_registry(tmp_path/'home','test')
            row=history.page(jobs_path=jobs,user_path=user,registry=registry)['items'][0]
            assert row['can_delete']
            result=controls.delete_active(item_id=row['id'],expected_version=row['control_version'],request_id=uuid4(),
                confirm=False,jobs_path=jobs,user_path=user,home=tmp_path/'home',registry=registry)
            assert result['deleted'] and not (media/'one.jpg.part').exists()
            assert (media/'archive.sqlite3').read_bytes()==b'preserved gallery archive'
            assert (media/'one.jpg.json').read_bytes()==b'preserved sidecar'
            child.wait(timeout=10);tracker.unregister()
    finally:
        if child and child.poll() is None:child.kill();child.wait(timeout=10)


def test_youtube_accepted_request_is_visible_before_any_worker_event(local,tmp_path):
    from modules.youtube import jobs
    source,media,connect=local
    job=jobs.create('https://youtu.be/abcdefghijk',source.source_id,request_id=uuid4())
    paths=[jobs.config.JOBS_DB_PATH,tmp_path/'user.sqlite']
    before={p:p.read_bytes() for p in paths}
    rows=read(jobs.config.JOBS_DB_PATH,tmp_path/'user.sqlite',jobs.config.FILES_DB_PATH,tmp_path)
    assert len(rows)==1
    row=rows[0]
    assert row['id']==history.identity('youtube',job['id'],'media')
    assert row['state']=='queued' and row['progress']==0 and row['attempt']==0
    assert row['date_source']=='request' and row['date_at']
    assert row['started_at'] is None and row['completed_at'] is None and not row['can_delete']
    assert {p:p.read_bytes() for p in paths}==before
    assert not list(media.iterdir())
    jobs.update(job['id'],status='downloading',title='Title found before bytes')
    row=read(jobs.config.JOBS_DB_PATH,tmp_path/'user.sqlite',jobs.config.FILES_DB_PATH,tmp_path)[0]
    assert row['name']=='Title found before bytes' and row['state']=='working' and row['progress'] is None
    assert row['started_at'] is None and row['date_source']=='request'
    with jobs.get_jobs_db() as db:
        youtube_history.event(db,job['id'],{'event':'file','name':'Title found before bytes','progress':25})
    row=read(jobs.config.JOBS_DB_PATH,tmp_path/'user.sqlite',jobs.config.FILES_DB_PATH,tmp_path)[0]
    assert row['started_at'] and row['date_source']=='transfer' and row['progress']==25


def test_saved_title_repairs_old_ledger_display_without_writing_saved_data(local,tmp_path):
    from modules.youtube import jobs
    source,media,connect=local
    job=jobs.create('https://youtu.be/abcdefghijk',source.source_id)
    jobs.update(job['id'],status='downloading')
    with jobs.get_jobs_db() as db:
        youtube_history.complete(db,job['id'],'video.webm')
    jobs.update(job['id'],title='Original YouTube 한글 title',status='complete')
    original=media/'video.webm';original.write_bytes(b'preserved media')
    before={p:p.read_bytes() for p in (jobs.config.JOBS_DB_PATH,tmp_path/'user.sqlite',original)}
    row=read(jobs.config.JOBS_DB_PATH,tmp_path/'user.sqlite',jobs.config.FILES_DB_PATH,tmp_path)[0]
    assert row['name']=='Original YouTube 한글 title' and row['progress']==100
    assert {p:p.read_bytes() for p in before}==before
    with jobs.get_jobs_db() as db:
        assert history._one(db,'SELECT name FROM kv_download_items')['name']=='video.webm'


def test_catalog_title_can_present_a_retained_ledger_without_operational_youtube_rows(tmp_path):
    jobs=tmp_path/'jobs.sqlite';user=tmp_path/'user.sqlite'
    with sqlite3.connect(jobs) as db:
        active(db,'youtube');youtube_history.complete(db,'job','video.webm')
    with sqlite3.connect(user) as db:
        capture(db,'youtube','item','youtube.download',{'legacy_job_ids':['job'],
            'record':{'id':'job','title':'Saved catalog title','path':'YouTube/video.webm','captured_at':'2027-01-16T00:00:00Z'}},key='job')
    before={p:p.read_bytes() for p in (jobs,user)}
    rows=read(jobs,user,tmp_path/'index.sqlite',tmp_path)
    assert len(rows)==1 and rows[0]['name']=='Saved catalog title' and rows[0]['date_source']=='transfer'
    assert {p:p.read_bytes() for p in before}==before


def test_title_and_request_rows_keep_retry_lineage_without_duplicates(local,tmp_path):
    from modules.youtube import jobs
    source,media,connect=local
    token=uuid4();job=jobs.create('https://youtu.be/abcdefghijk',source.source_id,request_id=token)
    assert jobs.create('https://youtu.be/abcdefghijk',source.source_id,request_id=token)['id']==job['id']
    jobs.cancel(job['id'])
    cancelled=read(jobs.config.JOBS_DB_PATH,tmp_path/'user.sqlite',jobs.config.FILES_DB_PATH,tmp_path)
    assert len(cancelled)==1 and cancelled[0]['state']=='cancelled' and cancelled[0]['started_at'] is None
    retried=jobs.retry(job['id'])
    rows=read(jobs.config.JOBS_DB_PATH,tmp_path/'user.sqlite',jobs.config.FILES_DB_PATH,tmp_path)
    assert len(rows)==1 and rows[0]['id']==cancelled[0]['id'] and rows[0]['state']=='queued'
    assert retried['id']!=job['id']

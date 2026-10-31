"""Only synthetic unfinished files and explicitly created child processes."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from services import operations,download_history as history,download_controls as controls,process_control
from files_base import sources,serving
from module_registry import build_registry


@pytest.fixture
def active(tmp_path):
    media=tmp_path/'media';media.mkdir()
    home=tmp_path/'profile';home.mkdir()
    user=tmp_path/'user.sqlite';jobs=tmp_path/'jobs.sqlite'
    with sqlite3.connect(user) as db:
        db.row_factory=sqlite3.Row
        sources.ensure_sources_schema(db)
        source=sources.register_source(db,media,'Fixture downloads')
        sources.update_source(db,source.source_id,role='youtube')
        db.commit()
    with sqlite3.connect(jobs) as db:
        operations.ensure_schema(db)
        operations.request(db,'youtube','job',kind='download',key='request',payload={})
        token=operations.claim(db,'youtube','job')
        item=history.record(db,'youtube','job',file_key='media',name='video.mp4',module_name='YouTube',progress=25)
    script="""from pathlib import Path
import sys,time
file=Path(sys.argv[1])
while True:
 with file.open('ab') as out:out.write(b'.')
 time.sleep(.02)
"""
    partial=media/'video.mp4.part'
    opts={'creationflags':0x08000000} if sys.platform=='win32' else {'start_new_session':True}
    child=subprocess.Popen([sys.executable,'-I','-c',script,str(partial)],**opts)
    birth=operations.process_identity(child.pid)
    try:
        for _ in range(200):
            if partial.exists() and partial.stat().st_size:break
            assert child.poll() is None;time.sleep(.02)
        assert partial.exists()
        with sqlite3.connect(jobs) as db:
            controls.bind_target(db,item,source_id=source.source_id,root=media,source_role='youtube',paths=[partial.name])
            controls.register_worker(db,'youtube','job',child.pid)
            row=history._one(db,'SELECT * FROM kv_download_items WHERE id=?',(item,))
        def cancel(row):
            with sqlite3.connect(jobs) as db:
                operations.transition(db,'youtube',row['operation_id'],state='cancelled')
        registry=build_registry(home,'test')
        registry=registry.replacing(replace(registry.require('youtube'),download_control_provider=lambda:controls.OwnerControls(cancel)))
        yield SimpleNamespace(root=tmp_path,media=media,home=home,user=user,jobs=jobs,source=source,item=item,
            row=row,version=controls.version(row),registry=registry,child=child,birth=birth,partial=partial)
    finally:
        if process_control.alive(child.pid,birth):process_control.stop(child.pid,birth)
        child.wait(timeout=10)


def remove(active,*,key=None,confirm=False,version=None,registry=None):
    return controls.delete_active(item_id=active.item,expected_version=version or active.version,
        request_id=key or uuid4(),confirm=confirm,jobs_path=active.jobs,user_path=active.user,
        home=active.home,registry=registry or active.registry)


def page(active):
    return history.page(jobs_path=active.jobs,user_path=active.user,registry=active.registry)['items']


def test_pause_resume_preserves_files_and_repeated_request_is_idempotent(active):
    key=uuid4()
    first=controls.set_paused(jobs_path=active.jobs,paused_value=True,request_id=key)
    assert first['paused'] is True
    before=active.partial.read_bytes();time.sleep(.15)
    assert active.partial.read_bytes()==before
    assert controls.set_paused(jobs_path=active.jobs,paused_value=True,request_id=key)==first
    assert controls.state(active.jobs)==first
    with pytest.raises(ValueError):controls.set_paused(jobs_path=active.jobs,paused_value=False,request_id=key)
    result=controls.set_paused(jobs_path=active.jobs,paused_value=False,request_id=uuid4())
    assert result['paused'] is False and result['revision']>first['revision']
    for _ in range(200):
        if active.partial.stat().st_size>len(before):break
        time.sleep(.02)
    assert active.partial.stat().st_size>len(before)
    assert page(active)[0]['can_delete']


def test_delete_stops_the_actual_writer_removes_only_partial_and_hides_entry(active):
    completed=active.media/'complete.mp4';completed.write_bytes(b'published')
    sidecar=active.media/'video.mp4.part.json';sidecar.write_bytes(b'preserved metadata')
    unrelated=active.media/'unrelated.part';unrelated.write_bytes(b'user bytes')
    key=uuid4();result=remove(active,key=key)
    assert result=={'deleted':True,'requires_confirmation':False,'file_count':1}
    assert not active.partial.exists() and not process_control.alive(active.child.pid,active.birth)
    assert completed.read_bytes()==b'published' and sidecar.read_bytes()==b'preserved metadata'
    assert unrelated.read_bytes()==b'user bytes' and page(active)==[]
    assert remove(active,key=key)==result
    with sqlite3.connect(active.jobs) as db:
        assert db.execute('SELECT state FROM kv_download_items').fetchone()[0]=='cancelled'


def test_multiple_temporary_media_require_count_confirmation(active):
    second=active.media/'audio.m4a.part';second.write_bytes(b'component')
    with sqlite3.connect(active.jobs) as db:
        controls.bind_target(db,active.item,source_id=active.source.source_id,root=active.media,source_role='youtube',paths=[second.name])
    key=uuid4();result=remove(active,key=key)
    assert result=={'deleted':False,'requires_confirmation':True,'file_count':2}
    assert active.partial.exists() and second.exists()
    assert not process_control.alive(active.child.pid,active.birth)
    result=remove(active,key=key,confirm=True)
    assert result['deleted'] and not active.partial.exists() and not second.exists()


def test_confirmation_cannot_be_bypassed_on_a_new_request(active):
    with pytest.raises(ValueError,match='Preview'):remove(active,confirm=True)
    assert process_control.alive(active.child.pid,active.birth) and active.partial.exists()


def test_file_changed_after_confirmation_is_preserved(active):
    second=active.media/'audio.m4a.part';second.write_bytes(b'component')
    with sqlite3.connect(active.jobs) as db:
        controls.bind_target(db,active.item,source_id=active.source.source_id,root=active.media,source_role='youtube',paths=[second.name])
    key=uuid4();assert remove(active,key=key)['requires_confirmation']
    second.write_bytes(b'user changed this file after preview')
    before=active.partial.read_bytes()
    with pytest.raises(ValueError,match='changed'):remove(active,key=key,confirm=True)
    assert active.partial.read_bytes()==before and second.read_bytes()==b'user changed this file after preview'


def test_source_visibility_change_refuses_file_removal(active):
    with sqlite3.connect(active.user) as db:
        db.row_factory=sqlite3.Row;sources.update_source(db,active.source.source_id,visible=False);db.commit()
    with pytest.raises(ValueError,match='source'):remove(active)
    assert active.partial.exists()


def test_registered_child_source_is_never_swept_from_parent(active):
    nested=active.media/'nested';nested.mkdir()
    file=nested/'child.mp4.part';file.write_bytes(b'child data')
    with sqlite3.connect(active.user) as db:
        db.row_factory=sqlite3.Row;sources.register_source(db,nested);db.commit()
    with sqlite3.connect(active.jobs) as db:
        db.execute('UPDATE kv_download_targets SET paths=? WHERE item_id=?',(json.dumps(['nested/child.mp4.part']),active.item));db.commit()
    with pytest.raises(ValueError,match='another source'):remove(active)
    assert file.read_bytes()==b'child data'


def test_sidecar_and_gallery_archive_refs_cannot_be_deleted(active):
    sidecar=active.media/'archive.sqlite3';sidecar.write_bytes(b'user archive')
    with sqlite3.connect(active.jobs) as db:
        db.execute('UPDATE kv_download_targets SET paths=? WHERE item_id=?',(json.dumps([sidecar.name]),active.item));db.commit()
    with pytest.raises(ValueError,match='Metadata'):remove(active)
    assert sidecar.read_bytes()==b'user archive'


def test_stale_version_cannot_stop_or_delete_the_new_attempt(active):
    with pytest.raises(ValueError,match='changed'):remove(active,version='0'*64)
    assert process_control.alive(active.child.pid,active.birth) and active.partial.exists()


def test_publication_winning_the_delete_race_keeps_completed_file(active):
    complete=active.media/'finished.mp4';complete.write_bytes(b'verified completed output')
    def finish(row):
        with sqlite3.connect(active.jobs) as db:
            history.record(db,'youtube','job',file_key='media',name=complete.name,module_name='YouTube',completed=True)
            controls.bind_target(db,active.item,source_id=active.source.source_id,root=active.media,source_role='youtube',
                paths=[complete.name],artwork=complete.name,published=True)
    registry=active.registry.replacing(replace(active.registry.require('youtube'),download_control_provider=lambda:controls.OwnerControls(finish)))
    with pytest.raises(ValueError,match='finished|changed'):remove(active,registry=registry)
    assert complete.read_bytes()==b'verified completed output'
    assert page(active)[0]['state']=='completed'


def test_absent_owner_is_readable_but_never_has_a_delete_action(active):
    from module_registry import ModuleRegistry
    registry=ModuleRegistry((active.registry.base,))
    rows=history.page(jobs_path=active.jobs,user_path=active.user,registry=registry)['items']
    assert len(rows)==1 and rows[0]['can_delete'] is False
    with pytest.raises(ValueError,match='owner'):remove(active,registry=registry)
    assert process_control.alive(active.child.pid,active.birth)


def test_read_state_never_initializes_a_missing_profile(tmp_path):
    path=tmp_path/'missing'/'jobs.sqlite'
    assert controls.state(path)=={'paused':False,'revision':0}
    assert not path.parent.exists()


def test_version_one_store_reads_passively_then_upgrades_additively(tmp_path):
    jobs=tmp_path/'jobs.sqlite'
    with sqlite3.connect(jobs) as db:
        for sql in history.SCHEMA:db.execute(sql)
        db.execute('INSERT INTO kv_download_schema VALUES(1,1)')
        db.execute('CREATE TABLE preserved(data BLOB)');db.execute("INSERT INTO preserved VALUES(x'00ff')");db.commit()
    before=jobs.read_bytes()
    registry=build_registry(tmp_path,'test')
    assert history.page(jobs_path=jobs,user_path=tmp_path/'missing.sqlite',registry=registry)['items']==[]
    assert jobs.read_bytes()==before
    with sqlite3.connect(jobs) as db:
        history.ensure_schema(db)
        assert db.execute('SELECT version FROM kv_download_schema').fetchone()[0]==2
        assert db.execute('SELECT data FROM preserved').fetchone()[0]==b'\0\xff'
        assert controls.check_schema(db)


def test_local_thumbnail_uses_existing_cache_without_modifying_source(active):
    from PIL import Image
    cover=active.media/'cover.png';Image.new('RGB',(20,16),'purple').save(cover)
    before=cover.read_bytes()
    with sqlite3.connect(active.jobs) as db:
        controls.bind_target(db,active.item,source_id=active.source.source_id,root=active.media,source_role='youtube',
            paths=[active.partial.name],artwork=cover.name)
    result=controls.thumbnail(item_id=active.item,jobs_path=active.jobs,user_path=active.user,home=active.home,registry=active.registry)
    assert result and result.is_file() and cover.read_bytes()==before


def test_repeated_worker_registration_retains_its_pause_receipt(active):
    controls.set_paused(jobs_path=active.jobs,paused_value=True,request_id=uuid4())
    with sqlite3.connect(active.jobs) as db:
        old=db.execute('SELECT suspended FROM kv_download_workers').fetchone()[0]
        controls.register_worker(db,'youtube','job',active.child.pid)
        assert db.execute('SELECT suspended FROM kv_download_workers').fetchone()[0]==old
    controls.set_paused(jobs_path=active.jobs,paused_value=False,request_id=uuid4())


def test_late_worker_registration_obeys_the_already_saved_pause_gate(active):
    with sqlite3.connect(active.jobs) as db:
        controls.unregister_worker(db,'youtube','job',active.child.pid)
    controls.set_paused(jobs_path=active.jobs,paused_value=True,request_id=uuid4())
    with sqlite3.connect(active.jobs) as db:
        controls.register_worker(db,'youtube','job',active.child.pid)
    before=active.partial.read_bytes();time.sleep(.12)
    assert active.partial.read_bytes()==before
    controls.set_paused(jobs_path=active.jobs,paused_value=False,request_id=uuid4())
    time.sleep(.12);assert active.partial.stat().st_size>len(before)


def test_new_attempt_refuses_an_old_prepared_delete(active):
    second=active.media/'audio.part';second.write_bytes(b'keep')
    with sqlite3.connect(active.jobs) as db:
        controls.bind_target(db,active.item,source_id=active.source.source_id,root=active.media,source_role='youtube',paths=[second.name])
    key=uuid4();assert remove(active,key=key)['requires_confirmation']
    with sqlite3.connect(active.jobs) as db:
        operations.retry(db,'youtube','job','new-request')
        operations.claim(db,'youtube','job')
    with pytest.raises(ValueError,match='changed'):remove(active,key=key,confirm=True)
    assert active.partial.exists() and second.read_bytes()==b'keep'


def test_retry_after_partial_unlink_failure_removes_only_unchanged_originals(active,monkeypatch):
    second=active.media/'audio.part';second.write_bytes(b'keep')
    with sqlite3.connect(active.jobs) as db:
        controls.bind_target(db,active.item,source_id=active.source.source_id,root=active.media,source_role='youtube',paths=[second.name])
    key=uuid4();assert remove(active,key=key)['requires_confirmation']
    unlink=Path.unlink
    def fail(path,*args,**kwargs):
        if path==second:raise PermissionError('synthetic locked file')
        return unlink(path,*args,**kwargs)
    with monkeypatch.context() as patcher:
        patcher.setattr(Path,'unlink',fail)
        with pytest.raises(PermissionError):remove(active,key=key,confirm=True)
    assert not active.partial.exists() and second.exists()
    assert remove(active,key=key,confirm=True)['deleted'] and not second.exists()


def test_source_role_change_refuses_removal(active):
    with sqlite3.connect(active.user) as db:
        db.row_factory=sqlite3.Row;sources.update_source(db,active.source.source_id,role='files');db.commit()
    with pytest.raises(ValueError,match='another source'):remove(active)
    assert active.partial.exists()


def test_linked_unfinished_file_does_not_remove_its_target(active):
    foreign=active.root/'foreign.mp4';foreign.write_bytes(b'user media')
    link=active.media/'linked.mp4.part'
    try:link.symlink_to(foreign)
    except OSError:pytest.skip('This Windows account cannot create symlinks')
    with sqlite3.connect(active.jobs) as db:
        db.execute('UPDATE kv_download_targets SET paths=? WHERE item_id=?',(json.dumps([link.name]),active.item));db.commit()
    with pytest.raises((ValueError,serving.ServeDenied)):remove(active)
    assert foreign.read_bytes()==b'user media' and link.is_symlink()


def test_exited_supervisor_recovery_stops_paused_orphan_without_deleting_files(active):
    controls.set_paused(jobs_path=active.jobs,paused_value=True,request_id=uuid4())
    with sqlite3.connect(active.jobs) as db:
        db.execute("UPDATE kv_download_workers SET supervisor_birth='obsolete'");db.commit()
        controls.recover_workers(db,'youtube')
        assert db.execute('SELECT count(*) FROM kv_download_workers').fetchone()[0]==0
    assert not process_control.alive(active.child.pid,active.birth) and active.partial.exists()
    assert controls.state(active.jobs)['paused']


@pytest.mark.parametrize('resuming',[False,True])
def test_failed_pause_change_rolls_back_previous_workers_and_gate(active,monkeypatch,resuming):
    if resuming:controls.set_paused(jobs_path=active.jobs,paused_value=True,request_id=uuid4())
    with sqlite3.connect(active.jobs) as db:
        original=operations.rows(db,'SELECT * FROM kv_download_workers')[0]
        db.execute('INSERT INTO kv_download_workers VALUES(?,?,?,?,?,?,?,?)',
            ('youtube','second',1,active.child.pid,active.birth,os.getpid(),'wrong-supervisor',original['suspended']));db.commit()
    before=active.partial.stat().st_size
    with pytest.raises(ValueError,match='supervisor'):
        controls.set_paused(jobs_path=active.jobs,paused_value=not resuming,request_id=uuid4())
    assert controls.state(active.jobs)['paused'] is resuming
    stopped=active.partial.stat().st_size
    time.sleep(.12)
    if resuming:assert active.partial.stat().st_size==stopped
    else:assert active.partial.stat().st_size>before


def test_control_api_validates_uuid_version_and_guards_completed_items(active,monkeypatch):
    import asyncio
    from fastapi import FastAPI
    from routers import downloads
    for key,value in [('JOBS_DB_PATH',active.jobs),('USER_DB_PATH',active.user),('SUITE_HOME',active.home),('MODULE_REGISTRY',active.registry)]:
        monkeypatch.setattr(downloads.config,key,value)
    app=FastAPI();app.include_router(downloads.router)
    async def call(method,path,body=None):
        data=json.dumps(body).encode() if body is not None else b''
        messages=[]
        async def receive():return {'type':'http.request','body':data,'more_body':False}
        async def send(message):messages.append(message)
        await app({'type':'http','asgi':{'version':'3.0'},'http_version':'1.1',
            'method':method,'scheme':'http','path':path,'raw_path':path.encode(),
            'query_string':b'','headers':[(b'content-type',b'application/json')],'server':('localhost',80),
            'client':('127.0.0.1',1234),'root_path':''},receive,send)
        return messages[0]['status'],json.loads(b''.join(m.get('body',b'') for m in messages[1:]))
    async def check():
        assert (await call('POST','/api/downloads/pause',{'request_id':'bad'}))[0]==422
        assert (await call('POST','/api/downloads/'+active.item+'/delete',{'request_id':str(uuid4()),'expected_version':'bad'}))[0]==422
        status,state=await call('POST','/api/downloads/pause',{'request_id':str(uuid4())})
        assert status==200 and state['paused']
        status,item=await call('GET','/api/downloads/'+active.item)
        assert item['can_delete']
        status,deleted=await call('POST','/api/downloads/'+active.item+'/delete',{'request_id':str(uuid4()),'expected_version':active.version})
        assert status==200 and deleted['deleted']
        assert (await call('GET','/api/downloads/'+active.item))[0]==404
    asyncio.run(check())

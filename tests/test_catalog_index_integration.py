"""Existing owner readers over prepared/rebuilt combined indexes, real scratch media."""
from contextlib import closing, contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import pytest
from fastapi import HTTPException
from PIL import Image
from starlette.responses import Response

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import config
import database
from services import catalog_index
from files_base import index, sources
from modules.files import _catalog_index as files_owner
from modules.danbooru.catalog_index import index_owner as dan_owner
from modules.ost import _catalog_index as music_owner
from modules.ost import router as music_router
# Load reader seams before fixtures patch database factories. Import order must
# not capture another fixture's temporary connection in a newly loaded router.
from routers import files as _files_boundary
from modules.video import router as _video_boundary
from modules.manga import router as _manga_boundary
from modules.language import router as _language_boundary
from tests.test_ost_module import catalog, publisher_music, publisher_music_tree
from tests.test_youtube_module import local
from tests.test_catalog_index import seed_danbooru, stores


def build(tmp_path, essential, registered, owners, legacy, *, name='index-attempt', archives=None):
    return catalog_index.prepare_index(tmp_path/name,essential_database=essential,
        sources=tuple(registered),owners=owners,legacy_paths=legacy,
        suite_home=config.SUITE_HOME,lock_directory=tmp_path/'index-state',archives=archives)


@pytest.mark.parametrize('publisher_music',['publisher','flat','untagged'],indirect=True)
def test_publisher_music_files_and_covers_survive_full_rebuild(publisher_music,monkeypatch):
    case=publisher_music
    for path in case.paths:
        assert music_router.metadata(Response(),case.source.source_id,path)['status']=='ready'
    tracks=music_router.tracks(0,240)
    albums=music_router.albums(0,120)
    tree=publisher_music_tree(case.root)
    with case.connect() as db:registered=sources.list_sources(db)
    result=build(case.root.parent,case.connect,registered,[files_owner(),music_owner()],
                 {'files':case.db,'ost':case.db})
    monkeypatch.setattr(config,'FILES_DB_PATH',result.candidate)
    assert music_router.tracks(0,240)==tracks
    assert music_router.albums(0,120)==albums
    first=music_router.cover(case.source.source_id,case.paths[0],300,None)
    body=Path(first.path).read_bytes()
    # All owner DBs are absent for the reconstruction proof.
    case.db.rename(case.db.with_suffix('.retained'))
    rebuilt=build(case.root.parent,case.connect,registered,[files_owner(),music_owner()],{},
                  archives=result.archives,name='rebuilt')
    monkeypatch.setattr(config,'FILES_DB_PATH',rebuilt.candidate)
    assert music_router.tracks(0,240)==tracks
    assert music_router.albums(0,120)==albums
    assert Path(music_router.cover(case.source.source_id,case.paths[0],300,None).path).read_bytes()==body
    assert publisher_music_tree(case.root)==tree


def test_files_video_manga_language_readers_and_source_guards(catalog,monkeypatch,tmp_path):
    from routers import files as files_router
    from modules.video import router as video
    from modules.manga import router as manga
    from modules.language import router as language
    _,essential,media,old=catalog
    fixtures={}
    with essential() as db:
        for role in ('video','manga','language'):
            root=tmp_path/role;root.mkdir()
            if role=='video':(root/'sample.mp4').write_bytes(b'generated listing fixture')
            elif role=='language':(root/'Study.md').write_text('# exact Korean 한국어\n')
            else:
                (root/'Chapter 1').mkdir()
                Image.new('RGB',(31,47),'blue').save(root/'Chapter 1/001.png')
            fixtures[role]=sources.register_source(db,root,role,role=role)
        registered=sources.list_sources(db)
    for owner in registered:
        with index.open_index(old) as db:index.scan_source(db,owner.source_id,owner.path)
    for module in (files_router,video,manga,language):
        monkeypatch.setattr(module,'get_user_db',essential)
    monkeypatch.setattr(video,'FILES_DB_PATH',old)
    baseline={
        'files':[v.model_dump() for v in files_router.browse(fixtures['video'].source_id,'')],
        'video':video.library('',0,120,None),
        'manga':manga.library('',0,60),
        'language':language.list_documents(fixtures['language'].source_id),
    }
    result=build(tmp_path,essential,registered,[files_owner()],{'files':old})
    monkeypatch.setattr(config,'FILES_DB_PATH',result.candidate)
    monkeypatch.setattr(video,'FILES_DB_PATH',result.candidate)
    assert [v.model_dump() for v in files_router.browse(fixtures['video'].source_id,'')]==baseline['files']
    assert video.library('',0,120,None)==baseline['video']
    assert manga.library('',0,60)==baseline['manga']
    assert language.list_documents(fixtures['language'].source_id)==baseline['language']
    assert manga.pages(fixtures['manga'].source_id,'Chapter 1','folder',False)=={'count':1}
    # Guarded original reads use current source state, not captured index paths.
    with essential() as db:sources.update_source(db,fixtures['manga'].source_id,visible=False)
    with pytest.raises(HTTPException):manga.pages(fixtures['manga'].source_id,'Chapter 1','folder',False)
    assert (tmp_path/'language/Study.md').read_text()=='# exact Korean 한국어\n'


def test_danbooru_api_result_ids_and_personal_references_survive_rebuild(stores,tmp_path,monkeypatch):
    from modules.danbooru.routers import images_media
    from modules.danbooru.user_schema import USER_SCHEMA
    from contextlib import contextmanager
    user,essential=stores
    old=tmp_path/'danbooru.sqlite';seed_danbooru(old)
    with closing(sqlite3.connect(user)) as db:
        db.executescript(USER_SCHEMA)
        db.execute("INSERT INTO favorites(file_id,file_path,pinned_at) VALUES(51,'/offline/old.jpg','exact original pin')")
        db.execute("INSERT INTO image_views(file_id,file_path,view_count,heart_spam_count) VALUES(51,'/offline/old.jpg',7,3)")
        db.commit()
    @contextmanager
    def data():
        with closing(sqlite3.connect(config.DATA_DB_PATH)) as db:
            db.row_factory=lambda cursor,row:dict(zip([c[0] for c in cursor.description],row))
            yield db
    from modules.danbooru import image_queries
    monkeypatch.setattr(images_media,'get_data_db',data)
    monkeypatch.setattr(image_queries,'get_data_db',data)
    monkeypatch.setattr(image_queries,'USER_DB_PATH',user)
    monkeypatch.setattr(database,'USER_DB_PATH',user)
    monkeypatch.setattr(images_media,'USER_DB_PATH',user)
    monkeypatch.setattr(config,'DATA_DB_PATH',old)
    before=images_media.list_images(offset=0,limit=60)
    favorites=images_media.list_images(offset=0,limit=60,favorites_only=True)
    assert favorites.total == 1
    result=build(tmp_path,essential,[],[files_owner(),dan_owner()],{'danbooru':old})
    monkeypatch.setattr(config,'DATA_DB_PATH',result.candidate)
    assert images_media.list_images(offset=0,limit=60)==before
    old.rename(tmp_path/'original-retained.sqlite')
    rebuilt=build(tmp_path,essential,[],[files_owner(),dan_owner()],{},
                  archives=result.archives,name='rebuilt')
    monkeypatch.setattr(config,'DATA_DB_PATH',rebuilt.candidate)
    assert images_media.list_images(offset=0,limit=60)==before
    assert images_media.list_images(offset=0,limit=60,favorites_only=True)==favorites
    with closing(sqlite3.connect(user)) as db:
        assert db.execute('SELECT pinned_at FROM favorites').fetchone()[0]=='exact original pin'
        assert db.execute('SELECT view_count,heart_spam_count FROM image_views').fetchone()==(7,3)
    with closing(sqlite3.connect(rebuilt.candidate)) as db:
        assert db.execute('SELECT id,downloaded_at FROM files').fetchone()==(51,'known old date')
        assert db.execute('SELECT id,raw_json FROM posts').fetchone()==(71,'{"unknown":"kept"}')
        assert db.execute("SELECT value FROM metadata WHERE key='unknown_migration'").fetchone()[0]=='exact evidence'


def test_completed_youtube_and_job_state_unchanged_by_combined_index(local,tmp_path,monkeypatch):
    from modules.youtube import jobs,router
    source,media,essential=local
    job=jobs.create('https://www.youtube.com/watch?v=abcdefghijk',source.source_id)
    jobs.update(job['id'],status='downloading')
    stage=media/'.keivotos/youtube'/job['id'];stage.mkdir(parents=True)
    (stage/'video.mp4').write_bytes(b'generated saved video')
    jobs.publish(job,stage,{'path':'video.mp4','title':'Saved exact title'})
    before=router.library('',0,60)
    with jobs.get_jobs_db() as db:state=catalog_index._owned_manifest(db,('youtube_downloads','kv_operations','kv_job_attempts'))
    with essential() as db:registered=sources.list_sources(db)
    original=config.FILES_DB_PATH
    result=build(tmp_path,essential,registered,[files_owner()],{'files':original})
    monkeypatch.setattr(config,'FILES_DB_PATH',result.candidate)
    assert router.library('',0,60)==before
    with jobs.get_jobs_db() as db:
        assert catalog_index._owned_manifest(db,('youtube_downloads','kv_operations','kv_job_attempts'))==state
    assert (media/jobs.get(job['id'])['path']).read_bytes()==b'generated saved video'


def test_stale_danbooru_worker_cannot_install_new_index_generation(tmp_path,monkeypatch):
    from services import operations
    from modules.danbooru.catalog_index import attempt_guard,prepare_replacement,install_replacement
    live=tmp_path/'live.sqlite';seed_danbooru(live)
    replacement=prepare_replacement(live)
    jobs=tmp_path/'jobs.sqlite'
    with closing(sqlite3.connect(jobs)) as db:
        operations.ensure_schema(db)
        operations.request(db,'danbooru','run',kind='tool',key='first',payload={})
        token=operations.claim(db,'danbooru','run')
        operations.transition(db,'danbooru','run',state='interrupted')
        operations.retry(db,'danbooru','run','new-request')
    monkeypatch.setenv('KEIVOTOS_DANBOORU_OPERATION',json.dumps({'id':'run','token':token,'jobs':str(jobs)}))
    original=live.read_bytes()
    with pytest.raises(operations.StaleAttempt):
        with attempt_guard():install_replacement(replacement)
    assert live.read_bytes()==original


@pytest.mark.parametrize('legacy_bundle',[False,True])
def test_shared_index_backup_restore_changes_only_selected_owner(stores,tmp_path,monkeypatch,legacy_bundle):
    import backup_bundle as backups
    user,essential=stores
    old=tmp_path/'old.sqlite';seed_danbooru(old)
    prepared=build(tmp_path,essential,[],[files_owner(),dan_owner()],{'danbooru':old})
    live=prepared.candidate
    monkeypatch.setattr(backups,'RECOVERY_DIR',tmp_path/'recovery')
    monkeypatch.setattr(backups,'USER_DB_PATH',user)
    monkeypatch.setattr(config,'JOBS_DB_PATH',tmp_path/'jobs.sqlite')
    selected={key:False for key in backups.BACKUP_COMPONENTS}
    selected['library_database']=True
    def components(path):
        return {**backups.COMPONENTS,'user_database':('databases/user.sqlite',user),
                'library_database':('databases/danbooru.sqlite',path)}
    monkeypatch.setattr(backups,'COMPONENTS',components(old if legacy_bundle else live))
    monkeypatch.setattr(backups,'get_backup_config',lambda:{
        'destination':str(tmp_path/'backups'),'components':selected})
    created=backups.create_backup_bundle(selected)
    import zipfile
    with zipfile.ZipFile(created['path']) as archive:
        manifest=json.loads(archive.read('manifest.json'))
        assert bool(manifest.get('index_owners')) is not legacy_bundle
        if not legacy_bundle:
            extracted=tmp_path/'inspected.sqlite'
            extracted.write_bytes(archive.read('databases/danbooru.sqlite'))
            with closing(sqlite3.connect(extracted)) as db:
                assert not db.execute("SELECT name FROM sqlite_master WHERE name IN ('files_index','files_audio_metadata','kv_index_meta')").fetchone()
    monkeypatch.setattr(backups,'COMPONENTS',components(live))
    with closing(sqlite3.connect(live)) as db:
        db.execute('UPDATE posts SET score=999')
        db.execute("INSERT INTO files_index(source_id,path,relative_path,parent,name) VALUES('kept','/kept/song.mp3','song.mp3','','newer Files fact')")
        db.commit()
    backups.restore_backup_bundle(Path(created['path']).name)
    with closing(sqlite3.connect(live)) as db:
        assert db.execute('SELECT score FROM posts').fetchone()[0] is None
        assert db.execute('SELECT name FROM files_index').fetchone()[0]=='newer Files fact'
        catalog_index.validate_index(db,[files_owner(),dan_owner()])
    with essential() as db:
        assert db.execute('SELECT note FROM personal').fetchone()['note']=='한국어'
    assert not (tmp_path/'jobs.sqlite').exists()


def test_combined_discovery_preserves_hidden_nested_and_offline_guards(catalog,monkeypatch,tmp_path):
    from routers import files as files_router
    from starlette.requests import Request
    _,essential,_,legacy=catalog
    parent=tmp_path/'linked';child=parent/'nested';child.mkdir(parents=True)
    (parent/'visible.txt').write_text('visible original')
    (child/'hidden.txt').write_text('hidden original')
    offline=tmp_path/'offline';offline.mkdir();(offline/'kept.txt').write_text('retained offline')
    with essential() as db:
        p=sources.register_source(db,parent,'Parent')
        c=sources.register_source(db,child,'Child')
        gone=sources.register_source(db,offline,'Offline')
    for source in (p,c,gone):
        with index.open_index(legacy) as db:index.scan_source(db,source.source_id,source.path)
    with essential() as db:
        sources.update_source(db,c.source_id,visible=False)
        registered=sources.list_sources(db)
    offline.rename(tmp_path/'offline-retained')
    result=build(tmp_path,essential,registered,[files_owner()],{'files':legacy})
    monkeypatch.setattr(config,'FILES_DB_PATH',result.candidate)
    monkeypatch.setattr(files_router,'get_user_db',essential)
    with index.open_index(result.candidate) as db:
        assert db.execute('SELECT available FROM files_index WHERE source_id=? AND name=?',(c.source_id,'hidden.txt')).fetchone()[0]==0
        assert db.execute('SELECT available FROM files_index WHERE source_id=? AND name=?',(gone.source_id,'kept.txt')).fetchone()[0]==0
        assert db.execute('SELECT available FROM files_index WHERE source_id=? AND name=?',(p.source_id,'visible.txt')).fetchone()[0]==1
    for source,name in ((c,'hidden.txt'),(gone,'kept.txt')):
        with pytest.raises(HTTPException):
            files_router.serve_file(Request({'type':'http','headers':[]}),source.source_id,name)
    assert (child/'hidden.txt').read_text()=='hidden original'
    assert (tmp_path/'offline-retained/kept.txt').read_text()=='retained offline'


def test_discovery_cancel_is_checked_inside_large_source(catalog,tmp_path):
    _,essential,media,legacy=catalog
    for number in range(100):(media/f'cancel-{number}.txt').write_text('generated')
    with essential() as db:registered=sources.list_sources(db)
    calls=0
    def cancelled():
        nonlocal calls
        calls+=1
        return calls>=5
    with pytest.raises(InterruptedError):
        catalog_index.prepare_index(tmp_path/'cancelled',essential_database=essential,
            sources=tuple(registered),owners=[files_owner()],legacy_paths={'files':legacy},
            suite_home=config.SUITE_HOME,lock_directory=tmp_path/'index-lock',cancelled=cancelled)
    assert calls<100
    assert not (tmp_path/'cancelled/index.sqlite').exists()
    assert len(list(media.glob('cancel-*.txt')))==100

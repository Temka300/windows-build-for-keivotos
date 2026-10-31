"""Portable selected-folder import into independent isolated profiles."""
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import hashlib,sqlite3,sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from files_base import sources,index,annotations
from module_registry import build_registry
from services import item_catalog,item_manifest as portable,collection_transfer as transfer
from services import catalog_index,storage_authority as authority


def profile(path):
    path.mkdir()
    registry=build_registry(path,'test')
    essential=path/'keivotos.sqlite'
    with closing(sqlite3.connect(essential)) as db:
        db.row_factory=sqlite3.Row
        sources.ensure_sources_schema(db);item_catalog.ensure_catalog_schema(db)
        annotations.ensure_annotations_schema(db)
    derived=path/'indexes/index.sqlite';derived.parent.mkdir()
    with closing(sqlite3.connect(derived)) as db:
        db.executescript(catalog_index.INDEX_SCHEMA)
        db.execute("INSERT INTO kv_index_meta VALUES(1,1,'test','{}')")
        db.commit();catalog_index.ensure_owner(db,registry.base.catalog_index_provider());db.commit()
    return SimpleNamespace(SUITE_HOME=path,USER_DB_PATH=essential,INDEX_DB_PATH=derived,
        MODULE_REGISTRY=registry,ARTIST_PROFILE_ARCHIVE_DIR=path/'artwork')


def packet(tmp_path,*,personal=False):
    source=profile(tmp_path/'original')
    root=tmp_path/'Album';(root/'Disc 2').mkdir(parents=True)
    (root/'Disc 2/07 한글.flac').write_bytes(b'publisher audio')
    (root/'cover.jpg').write_bytes(b'publisher cover')
    (root/'original.cue').write_bytes(b'publisher cue\r\n')
    with closing(sqlite3.connect(source.USER_DB_PATH)) as db:
        db.row_factory=sqlite3.Row
        registered=sources.register_source(db,root,'Album')
        annotations.upsert_annotation(db,subject_kind='file',
            content_hash=hashlib.md5(b'publisher audio').hexdigest(),source_id=registered.source_id,
            relative_path='Disc 2/07 한글.flac',description='associated personal note')
        with authority.connection(source.INDEX_DB_PATH) as derived:
            index.scan_source(derived,registered.source_id,root)
        context=portable.Context(registered,(registered,),source.SUITE_HOME,source.INDEX_DB_PATH,db,source.MODULE_REGISTRY)
        output=tmp_path/'packet'
        transfer.export_collection([context],output,request_id='export',include_personal=personal,configuration=source)
    return output,registered,root


@pytest.mark.parametrize('personal',[False,True])
def test_fresh_profile_uses_original_tree_and_optional_personal_records(tmp_path,personal):
    output,registered,original=packet(tmp_path,personal=personal)
    target=profile(tmp_path/'fresh');destination=tmp_path/'imported'
    preview=transfer.preview_import(output,destination,target,include_personal=personal)
    assert preview['files']==3 and not destination.exists()
    result=transfer.import_collection(output,destination,target,request_id='import',include_personal=personal)
    assert result['files']==3
    assert transfer.import_collection(output,destination,target,request_id='import',include_personal=personal)==result
    copied=destination/'roots'/registered.source_id
    assert {p.relative_to(copied).as_posix():p.read_bytes() for p in copied.rglob('*') if p.is_file()}=={
        p.relative_to(original).as_posix():p.read_bytes() for p in original.rglob('*') if p.is_file()}
    with authority.connection(target.USER_DB_PATH,readonly=True) as db:
        assert sources.get_source(db,registered.source_id).path==str(copied)
        assert db.execute('SELECT count(*) FROM files_annotations').fetchone()[0]==int(personal)
    with authority.connection(target.INDEX_DB_PATH,readonly=True) as db:
        assert db.execute('SELECT count(*) FROM files_index').fetchone()[0]>=3


def test_preview_refuses_identity_conflict_before_creating_destination(tmp_path):
    output,registered,_=packet(tmp_path)
    target=profile(tmp_path/'fresh')
    with authority.connection(target.USER_DB_PATH) as db:
        db.execute('INSERT INTO files_sources VALUES(?,?,?,?,?,?)',
            (registered.source_id,str(tmp_path/'elsewhere'),'Different','files',1,registered.added_at))
    with pytest.raises(ValueError,match='identity'):transfer.preview_import(output,tmp_path/'refused',target)
    assert not (tmp_path/'refused').exists()


def test_import_refuses_existing_foreign_destination_and_tampered_packet(tmp_path):
    output,registered,_=packet(tmp_path)
    target=profile(tmp_path/'fresh');destination=tmp_path/'mine';destination.mkdir()
    (destination/'mine').write_bytes(b'preserve')
    with pytest.raises(ValueError):transfer.import_collection(output,destination,target,request_id='refused')
    assert (destination/'mine').read_bytes()==b'preserve'
    (output/'roots'/registered.source_id/'cover.jpg').write_bytes(b'changed')
    with pytest.raises(ValueError):transfer.preview_import(output,tmp_path/'refused',target)
    assert not (tmp_path/'refused').exists()


def test_danbooru_selected_facts_favorites_and_raw_source_survive_index_loss(tmp_path):
    source=profile(tmp_path/'original')
    root=tmp_path/'Images';root.mkdir();media=root/'original.jpg';media.write_bytes(b'original')
    owner=source.MODULE_REGISTRY.require('danbooru')
    with authority.connection(source.INDEX_DB_PATH) as derived:
        catalog_index.ensure_owner(derived,owner.catalog_index_provider())
        derived.execute("INSERT INTO files(id,path,name,relative_path) VALUES(7,?,'original.jpg','original.jpg')",(str(media),))
        derived.execute("INSERT INTO posts(id,file_id,raw_json) VALUES(8,7,?)",('{"unknown":"preserved"}',))
        derived.execute("INSERT INTO tags VALUES(9,'artist','artist')")
        derived.execute("INSERT INTO post_tags VALUES(8,9)")
    with authority.connection(source.USER_DB_PATH) as db:
        db.executescript(owner.user_schema_provider())
        registered=sources.register_source(db,root,'Images',role='danbooru')
        db.execute("INSERT INTO favorites(file_id,file_path) VALUES(7,?)",(str(media),));db.commit()
        context=portable.Context(registered,(registered,),source.SUITE_HOME,source.INDEX_DB_PATH,db,source.MODULE_REGISTRY)
        output=tmp_path/'packet'
        transfer.export_collection([context],output,request_id='dan',include_personal=True,configuration=source)
    target=profile(tmp_path/'fresh');destination=tmp_path/'imported'
    transfer.import_collection(output,destination,target,request_id='import',include_personal=True)
    with authority.connection(target.INDEX_DB_PATH,readonly=True) as derived:
        path=derived.execute('SELECT path FROM files WHERE id=7').fetchone()[0]
        assert path==str(destination/'roots'/registered.source_id/'original.jpg')
        assert derived.execute('SELECT raw_json FROM posts').fetchone()[0]=='{"unknown":"preserved"}'
    with authority.connection(target.USER_DB_PATH) as db:
        assert db.execute('SELECT file_path FROM favorites').fetchone()[0]==path
        archive=catalog_index.latest_archive(db,'danbooru')
        assert archive
        with closing(sqlite3.connect(':memory:')) as regenerated:
            catalog_index.restore_owner(db,regenerated,archive,owner.catalog_index_provider())
            assert regenerated.execute('SELECT raw_json FROM posts').fetchone()[0]=='{"unknown":"preserved"}'


def test_import_request_cannot_change_personal_choice_on_retry(tmp_path):
    output,_,_=packet(tmp_path,personal=True)
    target=profile(tmp_path/'fresh');destination=tmp_path/'imported'
    transfer.import_collection(output,destination,target,request_id='import',include_personal=True)
    with pytest.raises(ValueError,match='another collection'):
        transfer.import_collection(output,destination,target,request_id='import',include_personal=False)


def test_restore_upgrades_old_essential_snapshot_and_coordinates_config(tmp_path):
    target=profile(tmp_path/'fresh')
    old=tmp_path/'legacy-user.sqlite'
    with closing(sqlite3.connect(old)) as db:
        db.row_factory=sqlite3.Row;sources.ensure_sources_schema(db);annotations.ensure_annotations_schema(db)
        annotations.upsert_annotation(db,subject_kind='dir',source_id='offline',relative_path='',
                                      description='old backup personal note')
    before=hashlib.sha256(old.read_bytes()).hexdigest()
    attempt=target.SUITE_HOME/'recovery/restoring';attempt.mkdir(parents=True)
    config=tmp_path/'saved-config.json';config.write_bytes(b'{"theme":"saved"}')
    current=target.SUITE_HOME/'config.json';current.write_bytes(b'{"theme":"current"}')
    databases,resources=authority.prepare_restore(target,[
        ('user_database',target.USER_DB_PATH,old),('runtime_configuration',current,config)],
        attempt,operational_present=False)
    authority.install_databases(target.SUITE_HOME,attempt,databases,resources=resources)
    with authority.connection(target.USER_DB_PATH,readonly=True) as db:
        assert db.execute('SELECT description FROM files_annotations').fetchone()[0]=='old backup personal note'
        assert db.execute('SELECT schema_version FROM kv_catalog_meta').fetchone()[0]==item_catalog.SCHEMA_VERSION
    assert current.read_bytes()==config.read_bytes()
    assert hashlib.sha256(old.read_bytes()).hexdigest()==before


def test_external_canonical_sidecar_is_exported_before_any_indexing(tmp_path):
    import json,base64
    from storage_layout import canonical_sidecar_path,LibraryRoot
    source=profile(tmp_path/'original');root=tmp_path/'Images';root.mkdir()
    media=root/'original.jpg';media.write_bytes(b'original bytes')
    source.DATA_ROOT=root;source.SIDECAR_DIR=source.SUITE_HOME/'sidecars'
    with authority.connection(source.USER_DB_PATH) as db:
        registered=sources.register_source(db,root,'Images',role='danbooru')
        path=canonical_sidecar_path(media,'.danbooru.json',root,source.SIDECAR_DIR,
                                   [LibraryRoot(registered.source_id,'Images',root)])
        raw=json.dumps({'local_file':{'path':str(media),'size':14},
            'post':{'id':81,'rating':'g'},'tags':{'general':['saved_tag']},
            'unknown-original-fact':'retained exact'}).encode()
        path.parent.mkdir(parents=True);path.write_bytes(raw)
        context=portable.Context(registered,(registered,),source.SUITE_HOME,source.INDEX_DB_PATH,db,source.MODULE_REGISTRY)
        output=tmp_path/'packet';transfer.export_collection([context],output,request_id='external',configuration=source)
    target=profile(tmp_path/'fresh');destination=tmp_path/'imported'
    transfer.import_collection(output,destination,target,request_id='import')
    with authority.connection(target.INDEX_DB_PATH,readonly=True) as db:
        assert db.execute('SELECT name FROM tags').fetchone()[0]=='saved_tag'
        captured=json.loads(db.execute("SELECT value FROM metadata WHERE key LIKE 'catalog-source:%'").fetchone()[0])
        assert base64.b64decode(captured['base64'])==raw
    assert path.read_bytes()==raw and media.read_bytes()==b'original bytes'


@pytest.mark.parametrize('owner',['youtube','twitter','manga','ost','video','language'])
def test_collection_roundtrip_uses_each_existing_owner_reader(tmp_path,monkeypatch,owner):
    from io import BytesIO
    from PIL import Image
    import json
    source=profile(tmp_path/'original');root=tmp_path/'Publisher collection';root.mkdir()
    data=BytesIO();Image.new('RGB',(12,12),'red').save(data,'PNG');image=data.getvalue()
    if owner=='youtube':
        from modules.youtube import metadata,saved_item,details
        (root/'original.mp4').write_bytes(b'original saved media');(root/'cover.png').write_bytes(image)
        url='https://www.youtube.com/watch?v=abcdefghijk'
        job={'id':'saved-job','source_id':'original-source','url':url,'video':'best','audio':'best','created_at':'2026-10-20 12:00:00'}
        capture=metadata.capture({'id':'abcdefghijk','channel':'Original channel'},url,'original.mp4')
        description='Transferred 한글 description\nExact line breaks';(root/'video.description').write_bytes(description.encode())
        snapshot=details.capture(dict(id='abcdefghijk',description=description,like_count=0),root,capture,engine_version='fixture')
        saved_item.prepare(root,job,{'path':'original.mp4','thumbnail':'cover.png','title':'Saved title','details':snapshot},capture)
    elif owner=='twitter':
        from tests.browser_twitter_fixture import write_capture
        from modules.twitter.portable import envelope
        relative=write_capture(root,photos={'photo-1.png':image},current_format=True)
        folder=(root/relative).parent;raw=(root/relative).read_bytes()
        portable.write(folder,envelope(folder,{'source_id':'original-source'},json.loads(raw),raw))
    elif owner=='manga':
        (root/'Series/Chapter 03').mkdir(parents=True);(root/'Series/Chapter 03/001.png').write_bytes(image)
    elif owner=='ost':
        from tests.browser_ost_fixture import write_audio
        (root/'Album/Disc 2').mkdir(parents=True);write_audio(root/'Album/Disc 2/07 Original.wav',1)
        (root/'Album/cover.png').write_bytes(image);(root/'Album/publisher.cue').write_bytes(b'publisher original cue\r\n')
    elif owner=='video':
        (root/'original.mp4').write_bytes(b'original video');(root/'original.ko.srt').write_bytes(b'original subtitle')
    else:(root/'한글.md').write_bytes('# Grammar\n\nExact authored note  \n'.encode())
    before={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with authority.connection(source.USER_DB_PATH) as db:
        registered=sources.register_source(db,root,'Publisher collection',role=owner)
        with authority.connection(source.INDEX_DB_PATH) as derived:index.scan_source(derived,registered.source_id,root)
        context=portable.Context(registered,(registered,),source.SUITE_HOME,source.INDEX_DB_PATH,db,source.MODULE_REGISTRY)
        if owner in ('youtube','twitter'):
            result=portable.discover(context)
            assert result['imported'] and not result['refused']
        output=tmp_path/'packet'
        transfer.export_collection([context],output,request_id='export',configuration=source)
    target=profile(tmp_path/'fresh');destination=tmp_path/'imported'
    transfer.import_collection(output,destination,target,request_id='import')
    copied=destination/'roots'/registered.source_id
    expected={name:raw for name,raw in before.items() if Path(name).name!='.prepare.lock'}
    assert {p.relative_to(copied).as_posix():p.read_bytes() for p in copied.rglob('*') if p.is_file()}==expected
    with authority.connection(target.USER_DB_PATH,readonly=True) as db:
        current=sources.get_source(db,registered.source_id)
        if owner=='youtube':
            from modules.youtube import metadata,details
            item=metadata.preserved_library(db)['items'][0];assert item['title']=='Saved title'
            assert metadata.preserved_media(db,'saved-job',source_id=current.source_id,source_root=copied,
                                             forbidden_roots=[target.SUITE_HOME],hash_assets=False)['assets']['media']
            facts,raw=details.read(copied,item,[target.SUITE_HOME])
            assert facts['description']==description and facts['like_count']==0 and facts['dislike_count']is None
            assert raw==description.encode()and list((target.SUITE_HOME/'preserved').rglob('*.txt'))
        elif owner=='twitter':
            from modules.twitter import archive
            assert archive.listing(target.INDEX_DB_PATH,[current],target.SUITE_HOME,0,10)['total']==1
        elif owner=='manga':
            from modules.manga import library
            assert library.chapter_metadata(current,'Series/Chapter 03','folder',target.INDEX_DB_PATH,[target.SUITE_HOME])['count']==1
        elif owner=='ost':
            from modules.ost import library
            assert library.directory(target.INDEX_DB_PATH,current,[current],target.SUITE_HOME,'Album/Disc 2',0,20)
        elif owner=='language':
            from modules.language import documents
            assert documents.read_document(current,target.SUITE_HOME,'한글.md')['content'].endswith('note  \n')
    assert not (target.SUITE_HOME/'state/jobs.sqlite').exists()
    assert {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}==before


@pytest.mark.parametrize('personal',[False,True])
def test_video_watch_transfer_retains_history_without_rebinding_copied_file(tmp_path,personal):
    from modules.video import progress
    from modules.video.user_schema import publish
    source=profile(tmp_path/'original');root=tmp_path/'Videos';root.mkdir()
    media=root/'episode.mp4';media.write_bytes(b'publisher video bytes')
    revision=progress.revision(media)
    with authority.connection(source.USER_DB_PATH) as db:
        publish(db);registered=sources.register_source(db,root,'Videos',role='video')
        db.execute('INSERT INTO video_watch_sessions VALUES(?,?,4,?)',(registered.source_id,'episode.mp4',revision))
        for version,position in [('a'*64,12.5),(revision,39.0)]:
            db.execute("INSERT INTO video_watch_progress VALUES(?,?,?,?,100,0,4,7,'observed')",(registered.source_id,'episode.mp4',version,position))
        db.commit()
        before=db.execute('SELECT * FROM video_watch_progress ORDER BY revision').fetchall()
        context=portable.Context(registered,(registered,),source.SUITE_HOME,source.INDEX_DB_PATH,db,source.MODULE_REGISTRY)
        output=tmp_path/'packet';transfer.export_collection([context],output,request_id='video-watch',include_personal=personal,configuration=source)
    target=profile(tmp_path/'fresh');destination=tmp_path/'imported'
    transfer.import_collection(output,destination,target,request_id='import',include_personal=personal)
    copied=destination/'roots'/registered.source_id/'episode.mp4'
    with authority.connection(target.USER_DB_PATH) as db:
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_watch_progress'").fetchone()
        actual=db.execute('SELECT * FROM video_watch_progress ORDER BY revision').fetchall() if exists else []
        assert [tuple(row) for row in actual]==([tuple(row) for row in before] if personal else [])
        if personal:
            assert progress.revision(copied)!=revision
            assert progress.read(db,registered.source_id,'episode.mp4',progress.revision(copied))['position'] is None
            assert db.execute('SELECT count(*) FROM video_watch_sessions').fetchone()[0]==1
    assert copied.read_bytes()==media.read_bytes()==b'publisher video bytes'



@pytest.mark.parametrize('personal',[False,True])
def test_video_metadata_source_facts_and_optional_choices_roundtrip(tmp_path,personal):
    from modules.video import metadata
    from modules.video.user_schema import publish
    source=profile(tmp_path/'original');root=tmp_path/'Videos';(root/'Show').mkdir(parents=True)
    (root/'Show/episode.mp4').write_bytes(b'original video')
    raw=b'{"title":"Ignored title","description":"Local description","genre":["Study"],"unknown":"preserve exact"}'
    (root/'Show/details.json').write_bytes(raw)
    with authority.connection(source.USER_DB_PATH) as db:
        publish(db);registered=sources.register_source(db,root,'Videos',role='video')
        with authority.connection(source.INDEX_DB_PATH) as files:index.scan_source(files,registered.source_id,root)
        db.commit();ctx=portable.Context(registered,(registered,),source.SUITE_HOME,source.INDEX_DB_PATH,db,source.MODULE_REGISTRY)
        metadata.refresh(ctx,'Show','series');state=metadata.details(ctx,'Show','series')
        metadata.save_choices(ctx,'Show','series',{'summary':'Personal description'},state['version'],state['source_revision'],str(__import__('uuid').uuid4()))
        output=tmp_path/'packet';transfer.export_collection([ctx],output,request_id='export-details',include_personal=personal,configuration=source)
    target=profile(tmp_path/'fresh');destination=tmp_path/'imported'
    transfer.import_collection(output,destination,target,request_id='import-details',include_personal=personal)
    with authority.connection(target.USER_DB_PATH) as db:
        current=sources.get_source(db,registered.source_id)
        ctx=portable.Context(current,(current,),target.SUITE_HOME,target.INDEX_DB_PATH,db,target.MODULE_REGISTRY)
        actual=metadata.details(ctx,'Show','series')
        assert actual['fields']['summary']==('Personal description' if personal else 'Local description')
        assert actual['fields']['tags']==['Study']
        assert actual['version']==int(personal)
        if personal:assert db.execute('SELECT count(*) FROM video_metadata_updates').fetchone()[0]==1
    assert (destination/'roots'/registered.source_id/'Show/details.json').read_bytes()==raw
    assert list((target.SUITE_HOME/'preserved').rglob('details.json'))
    assert (root/'Show/episode.mp4').read_bytes()==b'original video'


@pytest.mark.parametrize('personal',[False,True])
def test_manga_source_metadata_and_opt_in_personal_choices_survive_transfer(tmp_path,personal):
    from modules.manga import metadata,user_schema
    from tests.test_item_manifest_integration import png
    source=profile(tmp_path/'source');root=tmp_path/'Publisher';chapter=root/'Series/Chapter 1';chapter.mkdir(parents=True)
    (chapter/'1.png').write_bytes(png());raw='<ComicInfo><Summary>원래 설명</Summary><Tags>Source, 이야기</Tags><Future>exact</Future></ComicInfo>'.encode();(root/'Series/ComicInfo.xml').write_bytes(raw)
    with authority.connection(source.USER_DB_PATH) as db:
        registered=sources.register_source(db,root,'Publisher',role='manga');user_schema.publish(db)
        with authority.connection(source.INDEX_DB_PATH) as derived:index.scan_source(derived,registered.source_id,root)
        context=portable.Context(registered,(registered,),source.SUITE_HOME,source.INDEX_DB_PATH,db,source.MODULE_REGISTRY)
        metadata.refresh(context,'Series');state=metadata.details(context,'Series','series')
        metadata.save_choices(context,'Series','series',{'tags':['personal']},state['version'],state['source_revision'],'00000000-0000-4000-8000-000000000001')
        output=tmp_path/'packet';transfer.export_collection([context],output,request_id='manga-metadata',include_personal=personal,configuration=source)
    target=profile(tmp_path/'target');destination=tmp_path/'imported'
    transfer.import_collection(output,destination,target,request_id='manga-import',include_personal=personal)
    with authority.connection(target.USER_DB_PATH) as db:
        current=sources.get_source(db,registered.source_id);context=portable.Context(current,(current,),target.SUITE_HOME,target.INDEX_DB_PATH,db,target.MODULE_REGISTRY)
        state=metadata.details(context,'Series','series');assert state['fields']['summary']=='원래 설명'
        assert state['fields']['tags']==(['personal'] if personal else ['Source','이야기'])
    assert (root/'Series/ComicInfo.xml').read_bytes()==raw
    assert (destination/'roots'/registered.source_id/'Series/ComicInfo.xml').read_bytes()==raw
    assert any(p.read_bytes()==raw for p in target.SUITE_HOME.rglob('ComicInfo.xml'))

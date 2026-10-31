"""Existing owner readers after copied-item import into fresh isolated profiles."""
from io import BytesIO
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from uuid import uuid4
import zipfile
import pytest
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import config
from files_base import index,sources
from module_registry import build_registry
from services import item_catalog,item_manifest as portable
from tests.browser_twitter_fixture import write_capture
from tests.browser_ost_fixture import write_audio,flac_comments
from tests.test_youtube_metadata import local as youtube_env

@pytest.fixture
def profile(tmp_path,monkeypatch):
    home=tmp_path/'home';home.mkdir()
    db=sqlite3.connect(home/'user.sqlite');db.row_factory=sqlite3.Row
    sources.ensure_sources_schema(db);item_catalog.ensure_catalog_schema(db)
    registry=build_registry(home,'test')
    monkeypatch.setattr(config,'SUITE_HOME',home)
    monkeypatch.setattr(config,'USER_DB_PATH',home/'user.sqlite')
    monkeypatch.setattr(config,'FILES_DB_PATH',home/'base/files.sqlite')
    def context(root,owner):
        source=sources.register_source(db,root,'Copied originals',role=owner)
        with index.open_index(config.FILES_DB_PATH) as idx:index.scan_source(idx,source.source_id,root)
        return portable.Context(source,tuple(sources.list_sources(db)),home,config.FILES_DB_PATH,db,registry)
    yield context,db,home
    db.close()

def png():
    image=BytesIO();Image.new('RGB',(12,12),'red').save(image,'PNG');return image.getvalue()

def tree(root):
    return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}

@pytest.mark.parametrize('rich',[False,True])
def test_youtube_actual_writer_fresh_catalog_and_library_reader(profile,tmp_path,rich,monkeypatch):
    from modules.youtube import metadata,saved_item,details
    make,db,home=profile
    original=tmp_path/'original';original.mkdir();(original/'download.mp4').write_bytes(b'saved-media')
    (original/'poster.png').write_bytes(png())
    url='https://www.youtube.com/watch?v=abcdefghijk'
    job={'id':uuid4().hex,'source_id':'original-source','url':url,'video':'best','audio':'best',
         'created_at':'2026-10-20 12:00:00'}
    facts=metadata.capture({'id':'abcdefghijk','channel':'Original channel'},url,'download.mp4')
    result={'path':'download.mp4','thumbnail':'poster.png','title':'Saved title'}
    if rich:
        text='Complete 한글 description\nLine two';(original/'video.description').write_bytes(text.encode())
        result['details']=details.capture(dict(id='abcdefghijk',description=text,like_count=0),original,facts,engine_version='fixture')
    media,cover,digest=saved_item.prepare(original,job,result,facts)
    value=portable.read(original,hash_assets=True)[0]
    before=tree(original);copied=tmp_path/'portable copy';shutil.copytree(original,copied)
    context=make(copied,'youtube')
    assert portable.import_item(context)==value['item']['id']
    assert portable.import_item(context)==value['item']['id']
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='youtube_downloads'").fetchone()
    listed=metadata.preserved_library(db)
    assert listed['total']==1 and listed['items'][0]['title']=='Saved title'
    observed=metadata.preserved_media(db,job['id'],source_id=context.source.source_id,
        source_root=copied,forbidden_roots=context.forbidden,hash_assets=False)
    assert observed['assets']['media']['path']==media
    assert observed['record']['manifest_sha256']==digest
    assert (copied/media).read_bytes()==b'saved-media' and (copied/cover).read_bytes()==png()
    assert tree(original)==before and list((home/'preserved').rglob('*.png'))
    if rich:
        result,raw=details.read(copied,observed['record'],context.forbidden)
        assert result['description']==text and result['like_count']==0 and result['dislike_count']is None
        assert raw==text.encode()and list((home/'preserved').rglob('*.txt'))
    from contextlib import contextmanager
    from modules.youtube import _publish,channels
    _publish(db);db.commit()
    monkeypatch.setattr(config,'JOBS_DB_PATH',home/'state/jobs.sqlite')
    @contextmanager
    def connection():yield db
    page=channels.listing(connection,'',0,60)
    assert page['items']==[dict(id='unknown',name='Unknown channel',avatar_id=None,item_count=1)]
    assert channels.videos(connection,'unknown','Saved title',0,60)['items'][0]['id']==job['id']
    assert tree(original)==before and (copied/media).read_bytes()==b'saved-media'

@pytest.mark.parametrize('version',[1,2,3])
def test_twitter_legacy_owner_portable_copy_and_actual_feed(profile,tmp_path,version):
    from modules.twitter import archive
    from modules.twitter.portable import envelope
    make,db,home=profile
    original=tmp_path/'original';original.mkdir()
    relative=write_capture(original,photos={'photo-1.png':png()} if version>1 else None,current_format=version==3)
    folder=(original/relative).parent;raw=(original/relative).read_bytes();manifest=json.loads(raw)
    value=envelope(folder,{'source_id':'original-source'},manifest,raw);portable.write(folder,value)
    before=tree(original);copied=tmp_path/'copied post';shutil.copytree(folder,copied)
    context=make(copied,'twitter');identity=portable.import_item(context)
    assert identity==value['item']['id'] and portable.import_item(context)==identity
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='twitter_downloads'").fetchone()
    capture=archive.capture(context.source,context.registered,home,'manifest.json')
    assert capture['capture_id']==manifest['capture_id'] and capture['available']
    listed=archive.listing(context.index_path,context.registered,home,0,10,query='한글')
    assert listed['total']==1 and len(listed['items'])==1
    assert listed['items'][0]['raw_text']==capture['raw_text']
    assert tree(original)==before and list((home/'preserved').rglob('source.json'))

@pytest.mark.parametrize('kind',['folder','archive'])
def test_manga_single_chapter_export_and_existing_page_reader(profile,tmp_path,kind):
    from modules.manga import library
    make,db,home=profile
    original=tmp_path/'publisher';chapter=original/'Series/Chapter 03';chapter.mkdir(parents=True)
    if kind=='folder':
        (chapter/'001.png').write_bytes(png());(chapter/'002.png').write_bytes(png());selection='Series/Chapter 03'
    else:
        selection='Series/Chapter 03.cbz'
        with zipfile.ZipFile(original/selection,'x') as archive:
            archive.writestr('001.png',png());archive.writestr('002.png',png())
    raw='<ComicInfo><Summary>한국어 설명</Summary><Tags>Local, 이야기</Tags><Writer>Ignored author</Writer></ComicInfo>'.encode()
    if kind=='folder':(original/'Series/ComicInfo.xml').write_bytes(raw)
    else:
        with zipfile.ZipFile(original/selection,'a') as archive:archive.writestr('ComicInfo.xml',raw)
    context=make(original,'manga');before=tree(original)
    exported=portable.export_item(context,selection,tmp_path/'one chapter',request_id='chapter-request')
    imported=make(exported,'manga');identity=portable.import_item(imported)
    assert portable.read(exported)[0]['item']['id']==identity
    assert library.chapter_metadata(imported.source,selection,kind,imported.index_path,imported.forbidden)['count']==2
    assert library.catalog(imported.index_path,[imported.source])
    assert library.read_page_bytes(imported.source,selection,kind,1,imported.index_path,imported.forbidden)
    from modules.manga import metadata
    assert metadata.details(imported,'Series','series')['fields']==dict(summary='한국어 설명',tags=['Local','이야기'])
    metadata.refresh(imported,'Series')
    assert metadata.details(imported,'Series','series')['fields']==dict(summary='한국어 설명',tags=['Local','이야기'])
    assert any(asset['role']=='metadata' for asset in portable.read(exported)[0]['assets'])
    assert any(p.name=='ComicInfo.xml' and p.read_bytes()==raw for p in home.rglob('ComicInfo.xml'))
    assert tree(original)==before

@pytest.mark.parametrize('extension',['wav','flac'])
def test_music_track_original_layout_tags_and_covers(profile,tmp_path,extension):
    from modules.ost import library
    make,db,home=profile
    original=tmp_path/'publisher';disc=original/'Publisher album/Disc 2';disc.mkdir(parents=True)
    track=disc/('07 Original title.'+extension)
    if extension=='wav':write_audio(track,1)
    else:
        import imageio_ffmpeg,subprocess
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-hide_banner','-loglevel','error','-f','lavfi',
            '-i','sine=frequency=440:duration=0.2','-c:a','flac',str(track)],check=True,timeout=30)
        flac_comments(track,['TITLE=Publisher title','ALBUM=Original album','DISCNUMBER=2/3','TRACKNUMBER=7/10'])
    (disc/'cover.png').write_bytes(png());(disc/'publisher.log').write_bytes(b'publisher extra')
    context=make(original,'ost');selection=track.relative_to(original).as_posix();before=tree(original)
    exported=portable.export_item(context,selection,tmp_path/'one track',request_id='track-request')
    imported=make(exported,'ost');identity=portable.import_item(imported);value=portable.read(exported)[0]
    assert value['item']['id']==identity and value['item']['type']=='track'
    assert (exported/selection).read_bytes()==track.read_bytes()
    assert (exported/'Publisher album/Disc 2/cover.png').read_bytes()==png()
    assert not (exported/'Publisher album/Disc 2/publisher.log').exists()
    assert library.directory(imported.index_path,imported.source,imported.registered,home,'Publisher album/Disc 2',0,20)
    if extension=='flac':
        tags=value['extensions']['ost']['audio']['tags']
        assert tags['title']==['Publisher title'] and tags['album']==['Original album'] and tags['disc']==['2'] and tags['disc_total']==['3']
    assert tree(original)==before

def test_language_and_video_explicit_authoritative_files(profile,tmp_path):
    from modules.language import documents
    make,db,home=profile
    root=tmp_path/'notes';root.mkdir();(root/'한글.md').write_bytes('# Grammar\n\nExact notes  \n'.encode('utf-8'))
    context=make(root,'language')
    exported=portable.export_item(context,'한글.md',tmp_path/'copied note',request_id='note-request')
    imported=make(exported,'language');portable.import_item(imported)
    assert documents.read_document(imported.source,home,'한글.md')['content']=='# Grammar\n\nExact notes  \n'
    videos=tmp_path/'videos';videos.mkdir();(videos/'film.mp4').write_bytes(b'original video')
    (videos/'film.ko.srt').write_bytes(b'1\n00:00:00,000 --> 00:00:01,000\nsubtitle')
    context=make(videos,'video')
    exported=portable.export_item(context,'film.mp4',tmp_path/'copied video',request_id='video-request',
        options={'related':['film.ko.srt']})
    imported=make(exported,'video');portable.import_item(imported)
    assert (exported/'film.ko.srt').read_bytes()==(videos/'film.ko.srt').read_bytes()


def test_danbooru_managed_publication_retry_and_fresh_catalog_projection(profile,tmp_path):
    from types import SimpleNamespace
    from modules.danbooru import portable as owner,pipeline
    from services import operations,job_store_migration
    make,db,home=profile
    jobs=home/'state/jobs.sqlite';jobs.parent.mkdir()
    with sqlite3.connect(jobs) as state:
        job_store_migration.initialize(state)
        operations.request(state,'danbooru','managed-request',kind='tool',key='managed-request',payload={'tool_id':'download'})
        token=operations.claim(state,'danbooru','managed-request')
    operation={'jobs':str(jobs),'id':'managed-request','token':token}
    output=tmp_path/'new downloads'
    args=SimpleNamespace(root=tmp_path,user_db=home/'user.sqlite',json_suffix='.danbooru.json')
    stage=owner.staging(args,output,operation)
    media=stage/'12_original.png';media.write_bytes(png())
    raw=media.with_name(media.name+args.json_suffix)
    post={'id':12,'md5':hashlib.md5(png()).hexdigest(),'tag_string':'one two','tag_string_general':'one two',
          'rating':'g','width':12,'height':12,'score':5,'unknown_original':{'keep':'exact'}}
    raw.write_text(json.dumps(post),encoding='utf-8')
    staged=tree(stage)
    owner.publish_download(args,output,stage,operation)
    published=[p for p in output.iterdir() if not p.name.startswith('.')]
    assert len(published)==1
    value=portable.read(published[0],hash_assets=True)[0]
    assert value['item']['owner']=='danbooru' and tree(stage)==staged
    owner.publish_download(args,output,stage,operation)
    assert portable.read(published[0])[0]==value
    copied=tmp_path/'copied image';shutil.copytree(published[0],copied)
    context=make(copied,'danbooru')
    assert portable.import_item(context)==value['item']['id']
    target=context.registry.require('danbooru').database
    with sqlite3.connect(target) as data:
        assert data.execute('SELECT danbooru_post_id,score FROM posts').fetchone()==(12,5)
        assert data.execute('SELECT path FROM files').fetchone()[0]==str(copied/media.name)
        assert data.execute('SELECT count(*) FROM tags').fetchone()[0]==2
    assert (copied/raw.name).read_bytes()==raw.read_bytes()
    with sqlite3.connect(jobs) as state:operations.transition(state,'danbooru','managed-request',state='cancelled')
    with pytest.raises(operations.StaleAttempt):owner.publish_download(args,output,stage,operation)
    assert portable.read(published[0])[0]==value


@pytest.mark.parametrize('nested',[False,True])
def test_legacy_youtube_flat_export_and_reimport_without_jobs(profile,tmp_path,nested):
    from modules.youtube import metadata
    from modules.youtube.user_schema import register_catalog_publication
    make,db,home=profile
    root=tmp_path/'old library';root.mkdir()
    context=make(root,'youtube');job_id=uuid4().hex
    relative='YouTube/'+job_id+'/original.mp4';path=root/relative;path.parent.mkdir(parents=True)
    path.write_bytes(b'legacy original')
    item_catalog.ensure_library(db,namespace='files.source',legacy_key=context.source.source_id,kind='linked',display_name='Old library')
    record=dict(id=job_id,url='https://www.youtube.com/watch?v=abcdefghijk',source_id=context.source.source_id,
        root=str(root),video='best',audio='best',title='Original title',path=relative,thumbnail='',
        created_at='2026-09-02 12:34:56',status='complete',unknown_original='keep this in capture provenance')
    db.execute('BEGIN IMMEDIATE')
    register_catalog_publication(db,record,legacy_job_ids=[job_id],source_id=context.source.source_id,
        source_root=root,forbidden_roots=context.forbidden);db.commit()
    before=tree(root);container=tmp_path/'new container';container.mkdir()
    destination=container/'one item' if nested else container/'standalone'
    portable.export_item(context,relative,destination,request_id='legacy-save')
    new_home=tmp_path/'fresh home';new_home.mkdir()
    with sqlite3.connect(new_home/'user.sqlite') as fresh:
        fresh.row_factory=sqlite3.Row;sources.ensure_sources_schema(fresh);item_catalog.ensure_catalog_schema(fresh)
        new_root=container if nested else destination
        source=sources.register_source(fresh,new_root,'Relocated',role='youtube')
        manifest=('one item/' if nested else '')+portable.MANIFEST
        imported=portable.Context(source,(source,),new_home,new_home/'base/files.sqlite',fresh,build_registry(new_home,'test'))
        identity=portable.import_item(imported,manifest)
        assert identity==metadata.preserved_capture(db,job_id)['item_id']
        assert portable.import_item(imported,manifest)==identity
        listed=metadata.preserved_library(fresh)
        assert listed['total']==1 and listed['items'][0]['title']=='Original title'
        observed=metadata.preserved_media(fresh,job_id,source_id=source.source_id,source_root=new_root,
                                         forbidden_roots=imported.forbidden,hash_assets=False)
        assert (new_root/observed['record']['path']).read_bytes()==b'legacy original'
        assert listed['items'][0]['captured_at']==''
        assert not fresh.execute("SELECT 1 FROM sqlite_master WHERE name='youtube_downloads'").fetchone()
    assert tree(root)==before

def test_reexported_local_track_retains_intrinsic_identity(profile,tmp_path):
    make,db,home=profile
    root=tmp_path/'publisher';root.mkdir();write_audio(root/'original.wav',1)
    original=make(root,'ost')
    first=portable.export_item(original,'original.wav',tmp_path/'first copy',request_id='one')
    imported=make(first,'ost');identity=portable.import_item(imported)
    second=portable.export_item(imported,'original.wav',tmp_path/'second copy',request_id='two')
    assert portable.read(second)[0]['item']['id']==identity
    assert portable.read(second)[0]==portable.read(first)[0]

def test_application_managed_danbooru_pipeline_uses_local_transport_and_registers(profile,tmp_path,monkeypatch):
    from modules.danbooru import pipeline
    from services import operations,job_store_migration
    make,db,home=profile
    output=tmp_path/'managed library';output.mkdir();context=make(output,'danbooru')
    monkeypatch.setattr(config,'MODULE_REGISTRY',context.registry)
    jobs=home/'state/jobs.sqlite';jobs.parent.mkdir()
    with sqlite3.connect(jobs) as state:
        job_store_migration.initialize(state)
        operations.request(state,'danbooru','pipeline-request',kind='tool',key='pipeline-request',payload={})
        token=operations.claim(state,'danbooru','pipeline-request')
    envelope={'id':'pipeline-request','token':token,'jobs':str(jobs)}
    monkeypatch.setenv('KEIVOTOS_DANBOORU_OPERATION',json.dumps(envelope))
    monkeypatch.setattr(pipeline,'_LIBRARY_ROOTS',[])
    post={'id':99,'md5':hashlib.md5(png()).hexdigest(),'tag_string':'known_tag','tag_string_general':'known_tag','rating':'g'}
    real_popen=pipeline.subprocess.Popen
    def local_transport(command,**kwargs):
        values=json.loads(Path(command[command.index('--config')+1]).read_text())
        destination=Path(values['extractor']['base-directory'])
        script="""import sys,json,time
from pathlib import Path
root=Path(sys.argv[1])
(root/'99_original.png').write_bytes(bytes.fromhex(sys.argv[2]))
(root/'99_original.png.danbooru.json').write_text(sys.argv[3],encoding='utf-8')
print('GALLERY_PROGRESS:99',flush=True)
time.sleep(.1)
"""
        return real_popen([sys.executable,'-I','-c',script,str(destination),png().hex(),json.dumps(post)],**kwargs)
    monkeypatch.setattr(pipeline.subprocess,'Popen',local_transport)
    args=pipeline.build_parser().parse_args(['--root',str(tmp_path),'--user-db',str(home/'user.sqlite'),
        '--jobs-db',str(jobs),'--sidecar-dir',str(home/'sidecars'),'--gallery-dl-dir',str(home/'gallery-dl'),
        'download','--destination',str(output),'known_tag'])
    assert pipeline.run_download(args)==0
    bindings=portable.bindings(db,owner='danbooru',source_id=context.source.source_id)
    assert len(bindings)==1
    assert pipeline.run_download(args)==0
    assert portable.bindings(db,owner='danbooru',source_id=context.source.source_id)==bindings
    with sqlite3.connect(context.registry.require('danbooru').database) as data:
        assert data.execute('SELECT danbooru_post_id FROM posts').fetchall()==[(99,)]
    assert list((home/'sidecars').rglob('*.danbooru.json'))
    assert len(list(output.glob('99-*')))==1
    assert not [p for p in pipeline.media_files([output]) if '.keivotos' in p.relative_to(output).parts]
    with sqlite3.connect(jobs) as state:
        assert state.execute("SELECT publication FROM kv_operations WHERE owner='danbooru'").fetchone()[0]=='catalog-registered'


def test_new_youtube_keeps_existing_collection_and_rescans_exact_original_title(youtube_env):
    from modules.youtube import jobs,metadata
    source,root,connect=youtube_env
    with connect() as db:
        library_id=portable.collection_identity(db,source)
    job=jobs.create('https://www.youtube.com/watch?v=abcdefghijk',source.source_id)
    jobs.update(job['id'],status='downloading')
    stage=root/'.keivotos/youtube'/job['id'];stage.mkdir(parents=True)
    (stage/'original.mp4').write_bytes(b'completed generated media')
    title='  Original 한글\n\x00title  '
    capture=metadata.capture({'id':'abcdefghijk'},job['url'],'original.mp4')
    jobs.publish(job,stage,dict(path='original.mp4',title=title,capture=capture))
    job=jobs.get(job['id']);relative=job['bundle_path']+'/'+portable.MANIFEST
    value=portable.read(root,relative)[0]
    assert value['library']['id']==library_id
    with connect() as db:
        context=portable.Context(source,tuple(sources.list_sources(db)),jobs.config.SUITE_HOME,
            jobs.config.FILES_DB_PATH,db,build_registry(jobs.config.SUITE_HOME,'test'))
        assert portable.import_item(context,relative)==value['item']['id']
        assert metadata.preserved_library(db)['items'][0]['title']==title


def test_untyped_linked_danbooru_image_reimports_without_inventing_origin(profile,tmp_path,monkeypatch):
    make,db,home=profile
    monkeypatch.setattr(config,'SIDECAR_DIR',home/'sidecars')
    monkeypatch.setattr(config,'DATA_ROOT',tmp_path)
    original=tmp_path/'local images';original.mkdir();(original/'unknown.png').write_bytes(png())
    context=make(original,'danbooru');before=tree(original)
    output=portable.export_item(context,'unknown.png',tmp_path/'image copy',request_id='local-image')
    copied=make(output,'danbooru');portable.import_item(copied)
    with sqlite3.connect(copied.registry.require('danbooru').database) as data:
        assert data.execute('SELECT danbooru_post_id FROM posts').fetchone()==(None,)
        assert data.execute('SELECT downloaded_at FROM files').fetchone()==(None,)
    assert portable.read(output)[0]['origin']=={} and tree(original)==before

def test_malformed_linked_source_refuses_before_export_publication(profile,tmp_path,monkeypatch):
    make,db,home=profile
    monkeypatch.setattr(config,'SIDECAR_DIR',home/'sidecars')
    monkeypatch.setattr(config,'DATA_ROOT',tmp_path)
    original=tmp_path/'local images';original.mkdir();(original/'image.png').write_bytes(png())
    (original/'image.png.danbooru.json').write_bytes(b'{"post":{}}')
    context=make(original,'danbooru');before=tree(original)
    output=tmp_path/'refused copy'
    with pytest.raises(ValueError,match='post identity'):
        portable.export_item(context,'image.png',output,request_id='bad-source')
    assert not output.exists() and tree(original)==before


def test_portable_danbooru_rebuilds_local_items_without_media_hashes(profile,tmp_path,monkeypatch):
    from modules.danbooru import pipeline,catalog_index
    from modules.danbooru.schema import ensure_catalog_index_schema
    from services.catalog_index import IndexBuild
    make,db,home=profile
    monkeypatch.setattr(config,'SIDECAR_DIR',home/'sidecars');monkeypatch.setattr(config,'DATA_ROOT',tmp_path)
    original=tmp_path/'images';original.mkdir();(original/'local.png').write_bytes(png())
    context=make(original,'danbooru')
    output=portable.export_item(context,'local.png',tmp_path/'standalone',request_id='local')
    copied=make(output,'danbooru');portable.import_item(copied)
    monkeypatch.setattr(pipeline,'md5_file',lambda *_:pytest.fail('Derived rebuild hashed a media body'))
    with sqlite3.connect(':memory:') as rebuilt:
        ensure_catalog_index_schema(rebuilt)
        catalog_index.rebuild_index(IndexBuild(rebuilt,db,tuple(sources.list_sources(db)),home,lambda:False,'test'))
        assert rebuilt.execute('SELECT downloaded_at,local_md5 FROM files').fetchone()==(None,None)
        assert rebuilt.execute('SELECT danbooru_post_id FROM posts').fetchone()==(None,)


@pytest.mark.parametrize('generic,retry,nested,rich',[(False,False,False,False),(False,True,True,False),(True,True,False,False),(True,True,True,False),(True,True,True,True)])
def test_legacy_bundle_and_accepted_retry_export_keep_owner_bytes(profile,tmp_path,generic,retry,nested,rich):
    from modules.youtube import metadata,saved_item,details
    from modules.youtube.portable import identity
    from modules.youtube.user_schema import register_catalog_publication
    make,db,home=profile
    root=tmp_path/'saved library';root.mkdir()
    context=make(root,'youtube');first=uuid4().hex;accepted=uuid4().hex if retry else first
    bundle='YouTube/Channel/Original ['+first[:16]+']'
    stage=root/bundle;stage.mkdir(parents=True);(stage/'original.mp4').write_bytes(b'original bundle media')
    url='https://www.youtube.com/watch?v=abcdefghijk'
    job=dict(id=first,url=url,video='best',audio='best',created_at='2026-09-02 12:34:56')
    if generic:job['source_id']=context.source.source_id
    facts=metadata.capture({'id':'abcdefghijk','channel':'Channel'},url,'original.mp4')
    result=dict(path='original.mp4',title='Original title')
    if rich:
        description='Retried capture 한글 wording';(stage/'video.description').write_bytes(description.encode())
        result['details']=details.capture(dict(id='abcdefghijk',description=description,like_count=0),stage,facts,engine_version='fixture')
    media,cover,digest=saved_item.prepare(stage,job,result,facts)
    original_bytes=(stage/portable.MANIFEST).read_bytes()
    item_catalog.ensure_library(db,namespace='files.source',legacy_key=context.source.source_id,
                                kind='linked',display_name='Saved library')
    record=dict(id=accepted,url=url,source_id=context.source.source_id,root=str(root),video='best',audio='best',
        title='Original title',path=bundle+'/'+media,thumbnail='',created_at=job['created_at'],retry_of=first if retry else '',
        **facts,layout_version=1,bundle_path=bundle,manifest_sha256=digest)
    db.execute('BEGIN IMMEDIATE')
    register_catalog_publication(db,record,legacy_job_ids=[accepted,first] if retry else [first],
        source_id=context.source.source_id,source_root=root,forbidden_roots=context.forbidden)
    db.commit()
    before=tree(root);container=tmp_path/'relocated';container.mkdir()
    destination=container/'one item'
    portable.export_item(context,bundle,destination,request_id='bundle-copy')
    value=portable.read(destination,hash_assets=True)[0]
    assert value['item']['id']==identity(accepted)
    assert (destination/'source/youtube-owner.json').read_bytes()==original_bytes
    if retry:assert value['context']['parents'][0]['id']==identity(first)
    new_home=tmp_path/'fresh home';new_home.mkdir()
    with sqlite3.connect(new_home/'user.sqlite') as fresh:
        fresh.row_factory=sqlite3.Row;sources.ensure_sources_schema(fresh);item_catalog.ensure_catalog_schema(fresh)
        new_root=container if nested else destination
        source=sources.register_source(fresh,new_root,'Relocated',role='youtube')
        manifest=('one item/' if nested else '')+portable.MANIFEST
        imported=portable.Context(source,(source,),new_home,new_home/'base/files.sqlite',fresh,build_registry(new_home,'test'))
        assert portable.import_item(imported,manifest)==identity(accepted)
        assert portable.import_item(imported,manifest)==identity(accepted)
        listed=metadata.preserved_library(fresh)
        assert listed['total']==1 and listed['items'][0]['id']==accepted
        observed=metadata.preserved_media(fresh,accepted,source_id=source.source_id,source_root=new_root,
                                         forbidden_roots=imported.forbidden,hash_assets=False)
        assert (new_root/observed['record']['path']).read_bytes()==b'original bundle media'
        assert observed['assets']['manifest']['sha256']==digest
        assert not fresh.execute("SELECT 1 FROM sqlite_master WHERE name='youtube_downloads'").fetchone()
        if rich:
            data,raw=details.read(new_root,observed['record'],imported.forbidden)
            assert data['description']==description and data['like_count']==0 and raw==description.encode()
    assert tree(root)==before



def test_video_local_details_and_exact_sidecars_survive_single_item_export(profile,tmp_path):
    from modules.video import metadata
    make,db,home=profile
    root=tmp_path/'Publisher videos';(root/'Show/Season 1').mkdir(parents=True)
    (root/'Show/Season 1/Episode 1.mp4').write_bytes(b'publisher video')
    raw=b'{"title":"Original show","description":"Original description","genre":["Study"],"unknown":{"keep":42}}'
    (root/'Show/details.json').write_bytes(raw)
    nfo=b'<episodedetails><title>Original episode</title><tag>local tag</tag><unknown>kept</unknown></episodedetails>'
    (root/'Show/Season 1/Episode 1.nfo').write_bytes(nfo)
    context=make(root,'video');before=tree(root);target=tmp_path/'exported'
    portable.export_item(context,'Show/Season 1/Episode 1.mp4',target,request_id='video-details')
    manifest=portable.read(target,hash_assets=True)[0]
    assert (target/'Show/details.json').read_bytes()==raw
    assert (target/'Show/Season 1/Episode 1.nfo').read_bytes()==nfo
    copied=make(target,'video');portable.import_item(copied)
    result=metadata.details(copied,'Show/Season 1/Episode 1.mp4','video')
    assert result['fields']['summary']=='Original description' and 'title' not in result['fields']
    assert result['fields']['tags']==['local tag']
    assert tree(root)==before
    assert manifest['item']['owner']=='video'


def test_existing_native_video_keeps_unknown_facts_and_asset_names(profile,tmp_path):
    from modules.video import metadata
    make,db,home=profile
    source=tmp_path/'Native video';source.mkdir();(source/'video.mp4').write_bytes(b'original video')
    native=portable.new(item_id='native-video',owner='video',item_type='video',library_id='native-library',
        assets=[portable.asset('video.mp4',asset_id='native-media')],extensions={'video':{'title':'Native title','summary':'Native description','tags':['Native tag'],'unknown':{'original':True}}})
    portable.write(source,native);context=make(source,'video');portable.import_item(context)
    assert metadata.details(context,'video.mp4','video')['fields']=={'summary':'Native description','tags':['Native tag']}
    original=tree(source);destination=tmp_path/'native copy'
    portable.export_item(context,'video.mp4',destination,request_id='native-unchanged')
    copied=portable.read(destination)[0]
    assert copied['item']==native['item'] and copied['extensions']==native['extensions']
    assert [{key:asset[key] for key in ('id','path','role','position')} for asset in copied['assets']]==[{key:asset[key] for key in ('id','path','role','position')} for asset in native['assets']]
    assert copied['assets'][0]['sha256']==hashlib.sha256(b'original video').hexdigest() and tree(source)==original

"""Old/new coexistence and portable publication, using disposable local bytes."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_youtube_metadata import local, facts, image_bytes, URL, CID
from modules.youtube import jobs, layout, metadata, router
from files_base import sources, index


def staged(local,title='A saved song',capture=None,ext='mp4',video='best'):
    source,root,_=local
    job=jobs.create(URL,source.source_id,video=video);jobs.update(job['id'],status='downloading')
    stage=root/'.keivotos/youtube'/job['id'];stage.mkdir(parents=True)
    (stage/('original.'+ext)).write_bytes(b'preserved completed media')
    (stage/'cover.png').write_bytes(image_bytes())
    capture=capture if capture is not None else facts(avatar='.keivotos/channel-avatar.png',avatar_error='',media_ext=ext)
    if capture['avatar']:
        (stage/'.keivotos').mkdir();(stage/capture['avatar']).write_bytes(image_bytes())
    return job,stage,dict(path='original.'+ext,thumbnail='cover.png',title=title,capture=capture)


def save(local,**kwargs):
    job,stage,result=staged(local,**kwargs);jobs.publish(job,stage,result)
    return jobs.get(job['id'])


def test_description_survives_actual_registration_retry_without_acquisition(local,monkeypatch):
    from modules.youtube import details
    job,stage,result=staged(local);text='Original retry description\n한국어'
    (stage/'video.description').write_bytes(text.encode())
    result['details']=details.capture(dict(id='abcdefghijk',description=text,like_count=0),stage,result['capture'],engine_version='fixture')
    scan=jobs.index.scan_source
    monkeypatch.setattr(jobs.index,'scan_source',lambda *args,**kwargs:(_ for _ in()).throw(RuntimeError('injected registration failure')))
    with pytest.raises(RuntimeError):jobs.publish(job,stage,result)
    first=jobs.get(job['id']);_,root,_=local;manifest=(root/first['bundle_path']/'.keivotos/item.json').read_bytes()
    jobs.update(first['id'],status='failed');monkeypatch.setattr(jobs.index,'scan_source',scan)
    monkeypatch.setattr(jobs,'dependencies',lambda:pytest.fail('Registration retry requested downloader tools'))
    replacement=jobs.retry(first['id']);jobs.update(replacement['id'],status='downloading');jobs.resume_registration(replacement)
    current=jobs.get(replacement['id']);value,raw=details.read(root,current)
    assert current['status']=='complete'and current['retry_of']==first['id']
    assert raw==text.encode() and value['like_count']==0 and value['dislike_count']is None
    assert (root/current['bundle_path']/'.keivotos/item.json').read_bytes()==manifest


@pytest.mark.parametrize('title',['日本語の歌 한글 🎵','CON.txt','../a\\b:<c>?*|"\x00\n',' . ','長'*200,'a'*512])
def test_safe_portable_names_preserve_original_bytes_and_relative_facts(local,title):
    source,root,connect=local;job=save(local,title=title)
    parts=job['path'].split('/');assert len(parts)==4 and parts[0]=='YouTube'
    assert CID in parts[1] and 'abcdefghijk' in parts[2] and job['id'][:16] in parts[2]
    assert Path(job['path']).name=='video.mp4'
    assert (root/job['path']).read_bytes()==b'preserved completed media'
    assert (root/job['avatar']).read_bytes()==image_bytes()
    assert layout.units(root/job['bundle_path']/'.keivotos/channel-avatar.webp')<=layout.MAX_PATH
    for part in parts:layout.component(part)
    manifest=root/job['bundle_path']/'.keivotos/item.json';data=manifest.read_bytes();value=json.loads(data)
    assert hashlib.sha256(data).hexdigest()==job['manifest_sha256']
    assert value['format']=='keivotos.item' and value['version']==1
    assert value['item']['owner']=='youtube' and value['storage']=='managed'
    captured=value['extensions']['youtube']['saved']
    assert captured['capture']['avatar']=='.keivotos/channel-avatar.png'
    assert str(root).encode() not in data and captured['source_url']==value['origin']['url']==URL
    with index.open_index(jobs.config.FILES_DB_PATH) as db:
        assert not db.execute("SELECT 1 FROM files_index WHERE name='item.json' OR name='channel-avatar.png'").fetchone()
    assert jobs.published_output(job,root)['path']==job['path']


def test_channel_rename_duplicate_names_repeated_video_and_separate_sources(local):
    first=save(local);second=save(local,capture=facts(channel_name='A renamed channel'))
    third=save(local,capture=facts(channel_id='UC'+'b'*22,channel_url=metadata.channel_url('UC'+'b'*22)))
    assert first['bundle_path'].split('/')[1]==second['bundle_path'].split('/')[1]
    assert first['bundle_path']!=second['bundle_path']!=third['bundle_path']
    assert first['channel_name']=='Captured name' and second['channel_name']=='A renamed channel'
    _,root,connect=local
    other_root=root.parent/'other-media';other_root.mkdir()
    with connect() as db:other_source=sources.register_source(db,other_root)
    separate=save((other_source,other_root,connect),capture=facts(channel_name='Other source channel'))
    assert separate['source_id']!=first['source_id'] and (other_root/separate['path']).is_file()
    with connect() as db:assert db.execute('SELECT count(*) AS n FROM youtube_channel_folders').fetchone()['n']==3


@pytest.mark.parametrize('ext,video',[('webm','best'),('mp4','best'),('m4a','audio'),('flac','audio')])
def test_actual_media_formats_are_preserved(local,ext,video):
    job=save(local,ext=ext,video=video)
    assert job['path'].endswith(('/audio.' if video=='audio' else '/video.')+ext)
    assert job['media_ext']==ext and job['thumbnail'].endswith('/thumbnail.png')


def test_unknown_channel_has_explicit_unique_identity(local):
    capture=facts(channel_id='',channel_url='',channel_name='',avatar_error='No reliable channel ID.')
    first=save(local,capture=capture);second=save(local,capture=capture)
    assert '/Unknown channel [' in first['path'] and first['bundle_path']!=second['bundle_path']


def test_old_flat_and_new_captured_downloads_keep_identities_and_metadata(local):
    source,root,connect=local
    old,stage,result=staged(local);result.pop('capture');jobs.publish(old,stage,result)
    before=jobs.get(old['id']);original=(root/before['path']).read_bytes()
    new=save(local)
    assert before['layout_version']==0 and len(before['path'].split('/'))==3
    assert new['layout_version']==1 and jobs.get(old['id'])==before
    assert jobs.published_output(before,root)['path']==before['path']
    assert (root/before['path']).read_bytes()==original and router.library('',0,60)['total']==2


def test_case_only_channel_identifiers_and_existing_folders_never_merge(local):
    first=save(local,capture=facts(channel_name='same'))
    other='UC'+'A'*22
    second=save(local,capture=facts(channel_id=other,channel_url=metadata.channel_url(other),channel_name='SAME'))
    assert first['bundle_path'].split('/')[1].casefold()!=second['bundle_path'].split('/')[1].casefold()
    assert (local[1]/first['path']).read_bytes()==(local[1]/second['path']).read_bytes()


@pytest.mark.parametrize('hazard',['nested','alias','symlink','long-root','collision'])
def test_unsafe_or_occupied_destinations_preserve_staging(local,tmp_path,hazard):
    source,root,connect=local;job,stage,result=staged(local)
    with connect() as db:bundle=layout.assign(db,job,root,result['capture'],result['title']);db.commit()
    if hazard=='nested':
        (root/'YouTube').mkdir()
        with connect() as db:sources.register_source(db,root/'YouTube')
    elif hazard=='alias':
        (root/'youtube').mkdir()
    elif hazard=='symlink':
        outside=tmp_path/'outside';outside.mkdir();(root/'YouTube').symlink_to(outside,target_is_directory=True)
    elif hazard=='long-root':
        monkeyroot=root/('a'*100)/('b'*100);monkeyroot.mkdir(parents=True)
        with pytest.raises(ValueError,match='too long'):
            with connect() as db:layout.assign(db,job,monkeyroot,result['capture'],result['title'])
        return
    else:(root/bundle).mkdir(parents=True)
    with pytest.raises((ValueError,OSError)):jobs.publish(job,stage,result)
    assert (stage/'original.mp4').read_bytes()==b'preserved completed media'
    assert not jobs.get(job['id'])['path']


def test_short_saved_identity_collision_is_refused(local,monkeypatch):
    prefix='a'*16
    monkeypatch.setattr(jobs.uuid,'uuid4',lambda:SimpleNamespace(hex=prefix+'1'*16));first=save(local)
    monkeypatch.setattr(jobs.uuid,'uuid4',lambda:SimpleNamespace(hex=prefix+'2'*16))
    job,stage,result=staged(local)
    with pytest.raises(ValueError,match='already exists'):jobs.publish(job,stage,result)
    assert (local[1]/first['path']).read_bytes()==b'preserved completed media'
    assert (stage/'original.mp4').is_file()


@pytest.mark.parametrize('hazard',['manifest','foreign-id','parent','absolute','backslash','case','missing','nested'])
def test_registration_refuses_changed_bundle_evidence(local,hazard):
    source,root,connect=local;job=save(local);manifest=root/job['bundle_path']/'.keivotos/item.json'
    if hazard=='manifest':manifest.write_bytes(b'{}')
    elif hazard=='foreign-id':
        value=json.loads(manifest.read_bytes());value['saved_item_id']='f'*32
        data=json.dumps(value).encode();manifest.write_bytes(data);job['manifest_sha256']=hashlib.sha256(data).hexdigest()
    elif hazard in ('parent','absolute','backslash','case'):
        job['bundle_path']={'parent':'../elsewhere','absolute':str(root),'backslash':job['bundle_path'].replace('/','\\'),
                            'case':job['bundle_path'].replace('YouTube/','youtube/')}[hazard]
    elif hazard=='missing':(root/job['path']).unlink()
    else:
        with connect() as db:sources.register_source(db,(root/job['path']).parent)
    with pytest.raises((ValueError,OSError)):jobs.published_output(job,root)


def test_registration_retry_retains_bundle_and_manifest_without_acquisition(local,monkeypatch):
    original=jobs.index.scan_source
    monkeypatch.setattr(jobs.index,'scan_source',lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('index failure')))
    job,stage,result=staged(local)
    with pytest.raises(RuntimeError):jobs.publish(job,stage,result)
    first=jobs.get(job['id']);jobs.update(job['id'],status='failed')
    root=local[1];before={p.relative_to(root).as_posix():p.read_bytes() for p in (root/first['bundle_path']).rglob('*') if p.is_file()}
    monkeypatch.setattr(jobs.index,'scan_source',original)
    monkeypatch.setattr(jobs,'dependencies',lambda:pytest.fail('Retry requested acquisition'))
    second=jobs.retry(first['id']);jobs.update(second['id'],status='downloading');jobs.resume_registration(second)
    saved=jobs.get(second['id'])
    assert saved['status']=='complete' and saved['path']==first['path']
    assert all(saved[name]==first[name] for name in layout.COLUMNS)
    assert before=={p.relative_to(root).as_posix():p.read_bytes() for p in (root/first['bundle_path']).rglob('*') if p.is_file()}


def test_manifest_write_failure_retains_completed_staging(local,monkeypatch):
    from services import item_manifest
    job,stage,result=staged(local);original=item_manifest._create
    def disk_full(path,raw):
        if path.name=='item.json':raise OSError('disk full fixture')
        return original(path,raw)
    monkeypatch.setattr(item_manifest,'_create',disk_full)
    with pytest.raises(OSError,match='disk full'):jobs.publish(job,stage,result)
    assert (stage/'video.mp4').read_bytes()==b'preserved completed media' and not jobs.get(job['id'])['path']

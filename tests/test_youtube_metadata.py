"""Channel snapshots with exact identities; all acquisition is replaced locally."""
from contextlib import contextmanager
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import sys

import pytest
from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from modules.youtube import _publish, jobs, metadata, channel_art, router, worker
from files_base import sources

URL='https://www.youtube.com/watch?v=abcdefghijk'
CID='UC'+'a'*22


def image_bytes(kind='PNG'):
    stream=BytesIO();Image.new('RGB',(24,24),'#7290ab').save(stream,kind);return stream.getvalue()


def facts(**extra):
    return {**metadata.capture(dict(id='abcdefghijk',channel_id=CID,channel='Captured name',duration=12.5,
        upload_date='20261010',format_id='137+140',vcodec='avc1',acodec='mp4a'),URL,'video.mp4'),**extra}


@pytest.fixture
def local(tmp_path,monkeypatch):
    root=tmp_path/'media';root.mkdir();user=tmp_path/'user.sqlite'
    @contextmanager
    def connect():
        with sqlite3.connect(user) as db:
            db.row_factory=lambda cursor,row:dict(zip([col[0] for col in cursor.description],row))
            yield db
    with connect() as db:
        sources.ensure_sources_schema(db);_publish(db);source=sources.register_source(db,root)
    monkeypatch.setattr(jobs,'get_user_db',connect);monkeypatch.setattr(router,'get_user_db',connect)
    monkeypatch.setattr(jobs.config,'FILES_DB_PATH',tmp_path/'files.sqlite')
    monkeypatch.setattr(jobs.config,'SUITE_HOME',tmp_path/'home')
    monkeypatch.setattr(jobs.config,'JOBS_DB_PATH',tmp_path/'home/state/jobs.sqlite')
    monkeypatch.setattr(jobs,'dependencies',lambda:dict(ready=True,missing=[],ffmpeg='fixture'))
    jobs.recover();return source,root,connect


def publish(local, capture=None):
    source,root,_=local
    job=jobs.create(URL,source.source_id);jobs.update(job['id'],status='downloading')
    stage=root/'.keivotos/youtube'/job['id'];stage.mkdir(parents=True)
    (stage/'video.mp4').write_bytes(b'preserved video')
    capture=capture if capture is not None else facts(avatar='.keivotos/channel-avatar.png',avatar_error='')
    if capture['avatar']:
        (stage/'.keivotos').mkdir();(stage/capture['avatar']).write_bytes(image_bytes())
    jobs.publish(job,stage,dict(path='video.mp4',title='Saved',capture=capture))
    return jobs.get(job['id'])


def test_exact_video_facts_and_sanitized_optional_values():
    result=facts()
    assert result['channel_id']==CID and result['channel_url'].endswith(CID)
    assert result['channel_name']=='Captured name' and result['duration_seconds']==12.5
    assert result['media_ext']=='mp4' and result['upload_date']=='20261010'
    assert metadata.validate(result,URL,'video.mp4')==result
    for wrong in (None,{},dict(id='wrongwrong00')):
        with pytest.raises(ValueError,match='requested video'):metadata.capture(wrong,URL,'video.mp4')
    invalid=metadata.capture(dict(id='abcdefghijk',channel_id='../../x',channel='<script>\x00',duration=float('inf'),upload_date='20260230'),URL,'saved.webm')
    assert invalid['channel_id']==invalid['channel_url']==invalid['upload_date']==''
    assert invalid['channel_name']=='<script>' and invalid['duration_seconds'] is None and invalid['media_ext']=='webm'


@pytest.mark.parametrize('field,value',[
    ('video_id','wrongwrong00'),('channel_url','https://evil.test/'),('channel_id','../x'),
    ('duration_seconds',float('nan')),('duration_seconds',True),('captured_at','yesterday'),
    ('media_ext','webm'),('avatar','../avatar.png'),('avatar',[]),('metadata_version',True),
    ('upload_date','20260230'),('channel_name','name\x00'),('avatar_error',''),
])
def test_helper_metadata_refuses_malformed_facts(field,value):
    with pytest.raises(ValueError):metadata.validate(facts(**{field:value}),URL,'video.mp4')


@pytest.mark.parametrize('kind,extension',[('JPEG','jpg'),('PNG','png'),('WEBP','webp')])
def test_profile_format_comes_from_original_bytes(kind,extension):
    assert channel_art.image_type(image_bytes(kind))==extension


def test_profile_decode_bounds_and_animated_refusal(monkeypatch):
    with pytest.raises(ValueError):channel_art.image_type(b'<svg/>')
    with pytest.raises(ValueError):channel_art.image_type(b'x'*(channel_art.MAX_BYTES+1))
    monkeypatch.setattr(channel_art,'MAX_PIXELS',100)
    with pytest.raises(ValueError):channel_art.image_type(image_bytes())
    monkeypatch.setattr(channel_art,'MAX_PIXELS',4096*4096)
    stream=BytesIO();Image.new('RGB',(8,8),'red').save(stream,'PNG',save_all=True,append_images=[Image.new('RGB',(8,8),'blue')])
    with pytest.raises(ValueError):channel_art.image_type(stream.getvalue())


@pytest.mark.parametrize('url',[
    'http://yt3.ggpht.com/a','https://yt3.ggpht.com.evil/a','https://localhost/a',
    'https://yt3.ggpht.com@127.0.0.1/a','https://yt3.ggpht.com:444/a',
    'https://yt3.ggpht.com/a#fragment','https://yt3.ggpht.com/a\n',
])
def test_profile_url_and_redirect_guards(url):
    with pytest.raises(ValueError):channel_art.remote_url(url)
    with pytest.raises(ValueError):channel_art.Redirects().redirect_request(None,None,302,'',{},url)


@pytest.mark.parametrize('mode',['ok','length','body','redirect'])
def test_avatar_http_limits_and_final_redirect(monkeypatch,mode):
    class Response(BytesIO):
        url='https://yt3.ggpht.com/avatar' if mode!='redirect' else 'https://127.0.0.1/private'
        headers={'Content-Length':str(channel_art.MAX_BYTES+1)} if mode=='length' else {}
    class Opener:
        def open(self,request,timeout):
            assert timeout==8 and request.headers.get('Accept-encoding')=='identity'
            return Response(b'x'*(channel_art.MAX_BYTES+1) if mode=='body' else image_bytes())
    monkeypatch.setattr(channel_art,'build_opener',lambda *_:Opener())
    if mode=='ok':assert channel_art.read_remote('https://yt3.googleusercontent.com/avatar')==image_bytes()
    else:
        with pytest.raises(ValueError):channel_art.read_remote('https://yt3.googleusercontent.com/avatar')


def test_published_metadata_survives_crash_before_path_ack(local,monkeypatch):
    original=jobs.update
    def update(identity,**values):
        if 'path' in values:raise RuntimeError('path acknowledgement interrupted')
        original(identity,**values)
    monkeypatch.setattr(jobs,'update',update)
    with pytest.raises(RuntimeError):publish(local)
    source,root,connect=local
    with jobs.get_jobs_db() as db:first=dict(db.execute('SELECT * FROM youtube_downloads').fetchone())
    assert not first['path'] and first['metadata_version']==1 and (root/first['avatar']).is_file()
    monkeypatch.setattr(jobs,'update',original);jobs.update(first['id'],status='failed')
    retry=jobs.retry(first['id']);jobs.update(retry['id'],status='downloading');jobs.resume_registration(retry)
    assert jobs.get(retry['id'])['channel_name']=='Captured name'
    assert router.channel_avatar(retry['id']).body==image_bytes()


def test_channel_lookup_never_consumes_feed_or_selects_video_cover(monkeypatch):
    import yt_dlp
    class NeverIterate:
        def __iter__(self):pytest.fail('Channel entries were enumerated')
    class Engine:
        def __init__(self,options):
            assert options['skip_download'] and options['extract_flat'] and options['lazy_playlist']
            assert options['playlist_items']=='0' and options['js_runtimes']=={} and not options['remote_components']
            assert options['socket_timeout']==8 and options['extractor_retries']==0
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def extract_info(self,url,**kwargs):
            assert url==metadata.channel_url(CID)+'/about'
            assert kwargs==dict(download=False,process=False,ie_key='YoutubeTab')
            return dict(id=CID,channel_id=CID,entries=NeverIterate(),thumbnails=[
                dict(id='banner_uncropped',url='https://yt3.ggpht.com/banner'),
                dict(id='video',url='https://yt3.ggpht.com/cover'),
                dict(id='avatar_uncropped',url='https://yt3.googleusercontent.com/avatar')])
    monkeypatch.setattr(yt_dlp,'YoutubeDL',Engine)
    assert channel_art.lookup(CID,None)=='https://yt3.googleusercontent.com/avatar'


@pytest.mark.parametrize('mode',['missing','wrong_author','request_budget','response_budget'])
def test_channel_lookup_limits_and_exact_author(monkeypatch,mode):
    import yt_dlp
    class Engine:
        def __init__(self,*args):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def urlopen(self,request):return BytesIO(b'x'*(8*1024*1024+1)) if mode=='response_budget' else BytesIO(b'{}')
        def extract_info(self,*args,**kwargs):
            if mode=='request_budget':
                for _ in range(5):self.urlopen(None)
            if mode=='response_budget':self.urlopen(None).read()
            return dict(id='UC'+'b'*22 if mode=='wrong_author' else CID,thumbnails=[])
    monkeypatch.setattr(yt_dlp,'YoutubeDL',Engine)
    with pytest.raises(ValueError,match={'missing':'not supplied','wrong_author':'requested author','request_budget':'request limit','response_budget':'response limit'}[mode]):
        channel_art.lookup(CID,None)


def test_avatar_failure_is_optional_and_never_substitutes_thumbnail(tmp_path,monkeypatch):
    (tmp_path/'thumbnail.png').write_bytes(image_bytes())
    monkeypatch.setattr(channel_art,'lookup',lambda *_:(_ for _ in ()).throw(ValueError('Channel response does not match the requested author')))
    result=channel_art.capture(facts(),tmp_path,None)
    assert not result['avatar'] and 'requested author' in result['avatar_error']
    assert list(tmp_path.iterdir())==[tmp_path/'thumbnail.png']


def test_profile_write_checks_directory_identity_after_lookup(tmp_path,monkeypatch):
    directory=tmp_path/'capture';directory.mkdir();preserved=tmp_path/'preserved'
    def lookup(*_):
        directory.rename(preserved);directory.mkdir()
        return 'https://yt3.ggpht.com/avatar'
    monkeypatch.setattr(channel_art,'lookup',lookup);monkeypatch.setattr(channel_art,'read_remote',lambda *_:image_bytes())
    result=channel_art.capture(facts(),directory,None)
    assert not result['avatar'] and result['avatar_error']
    assert list(directory.iterdir())==list(preserved.iterdir())==[]


def test_captured_names_formats_and_avatar_survive_retry_and_restart(local,monkeypatch):
    source,root,connect=local
    original=jobs.index.scan_source
    monkeypatch.setattr(jobs.index,'scan_source',lambda *_args,**_kw:(_ for _ in ()).throw(RuntimeError('registration fixture')))
    with pytest.raises(RuntimeError):publish(local)
    with jobs.get_jobs_db() as db:first=dict(db.execute('SELECT * FROM youtube_downloads').fetchone())
    assert first['failure_phase']=='integration' and first['channel_name']=='Captured name'
    jobs.update(first['id'],status='failed')
    avatar=root/first['avatar'];original_bytes=avatar.read_bytes()
    monkeypatch.setattr(jobs.index,'scan_source',original)
    monkeypatch.setattr(jobs,'dependencies',lambda:pytest.fail('Registration retry requested acquisition'))
    second=jobs.retry(first['id']);jobs.recover();third=jobs.retry(second['id'])
    jobs.update(third['id'],status='downloading');jobs.resume_registration(third)
    current=jobs.get(third['id'])
    assert {name:current[name] for name in metadata.COLUMNS}=={name:first[name] for name in metadata.COLUMNS}
    assert current['status']=='complete' and avatar.read_bytes()==original_bytes
    response=router.channel_avatar(third['id'])
    assert response.body==original_bytes and response.media_type=='image/png'
    assert response.headers['x-content-type-options']=='nosniff'
    assert jobs.get(first['id'])['channel_name']=='Captured name'


def test_channel_names_are_snapshots_not_shared_identity(local):
    first=publish(local)
    second=publish(local,facts(channel_name='Renamed channel',avatar_error='Missing artwork.'))
    third=publish(local,facts(channel_id='UC'+'b'*22,channel_url=metadata.channel_url('UC'+'b'*22),channel_name='Captured name'))
    assert first['channel_name']=='Captured name' and second['channel_name']=='Renamed channel'
    assert first['channel_id']==second['channel_id']!=third['channel_id']
    assert router.library('',0,60)['total']==3


def test_missing_artwork_still_completes_and_legacy_rows_do_not_fetch(local,monkeypatch):
    job=publish(local,facts())
    assert job['status']=='complete' and job['avatar_error'] and not job['avatar']
    monkeypatch.setattr(channel_art,'lookup',lambda *_:pytest.fail('Offline read requested network'))
    assert router.library('',0,60)['items'][0]['channel_id']==CID
    with pytest.raises(Exception) as exc:router.channel_avatar(job['id'])
    assert exc.value.status_code==404


@pytest.mark.parametrize('hazard',['hidden','nested','symlink','missing','corrupt','path'])
def test_profile_route_rechecks_source_and_asset(local,tmp_path,hazard):
    source,root,connect=local;job=publish(local)
    if hazard=='hidden':
        with connect() as db:db.execute('UPDATE files_sources SET visible=0 WHERE source_id=?',(source.source_id,));db.commit()
    elif hazard=='nested':
        with connect() as db:sources.register_source(db,(root/job['path']).parent)
    elif hazard=='symlink':
        other=tmp_path/'other.png';other.write_bytes(image_bytes());asset=root/job['avatar'];asset.unlink();asset.symlink_to(other)
    elif hazard=='missing':(root/job['avatar']).unlink()
    elif hazard=='corrupt':(root/job['avatar']).write_bytes(b'<html>')
    else:jobs.update(job['id'],avatar='../outside.png')
    with pytest.raises(Exception) as exc:router.channel_avatar(job['id'])
    assert exc.value.status_code==404


def test_additive_columns_keep_all_old_fields_and_are_repeatable(local):
    source,root,connect=local;job=publish(local,facts())
    with jobs.get_jobs_db() as db:
        before=dict(db.execute('SELECT * FROM youtube_downloads').fetchone())
        with connect() as legacy_db:_publish(legacy_db);_publish(legacy_db)
        assert dict(db.execute('SELECT * FROM youtube_downloads').fetchone())==before


@pytest.mark.parametrize('description',[None,'Full 한국어 description\nSecond line'])
def test_worker_complete_includes_facts_with_optional_avatar(tmp_path,monkeypatch,capsys,description):
    import yt_dlp
    class Engine:
        def __init__(self,options):
            self.options=options
            assert options['writedescription']and not options['writeinfojson']and options['js_runtimes']=={}and not options['remote_components']
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def extract_info(self,*args,**kwargs):
            path=tmp_path/'video.mp4';path.write_bytes(b'media');self.options['post_hooks'][0](str(path))
            if description is not None:(tmp_path/'video.description').write_bytes(description.encode())
            return dict(id='abcdefghijk',channel_id=CID,channel='Name',description=description,like_count=0)
    monkeypatch.setattr(yt_dlp,'YoutubeDL',Engine)
    monkeypatch.setattr(channel_art,'lookup',lambda *_:'https://yt3.ggpht.com/art.jpg')
    monkeypatch.setattr(channel_art,'read_remote',lambda *_:image_bytes())
    worker.download(dict(directory=str(tmp_path),url=URL,video='best',audio='best',ffmpeg='fixture'))
    result=json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result['capture']['avatar']=='.keivotos/channel-avatar.png' and not result['capture']['avatar_error']
    assert result['thumbnail']=='' and result['capture']['video_id']=='abcdefghijk'
    assert (tmp_path/result['capture']['avatar']).read_bytes()==image_bytes()
    from modules.youtube import details
    assert set(result['capture'])==set(metadata.COLUMNS)and result['details']['like_count']==0 and result['details']['dislike_count']is None
    assert 'Full 한국어'not in json.dumps(result,ensure_ascii=False)
    if description is not None:assert details.asset_bytes(tmp_path,result['details'])==description.encode()

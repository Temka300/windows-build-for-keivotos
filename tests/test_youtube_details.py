"""Captured text/counts, portable preservation and offline reads; no provider IO."""
import copy
import hashlib
import json
from pathlib import Path

import pytest
from fastapi import HTTPException, Response

from tests.test_youtube_metadata import local, facts, URL
from tests.test_youtube_layout import staged
from modules.youtube import details, jobs, metadata, portable, router, saved_item
from services import item_manifest


def snapshot(directory, text='한글 description\nSecond line <img src="https://invalid.test/x">', **extra):
    info=dict(id='abcdefghijk',description=text,like_count=0,extractor_key='Youtube',**extra)
    if isinstance(text,str):(directory/'video.description').write_bytes(text.encode('utf-8'))
    return details.capture(info,directory,facts(),engine_version='fixture')


def rich_save(local, text='한글 description\nSecond line <img src="https://invalid.test/x">', *,video='best',ext='mp4',**extra):
    job,stage,result=staged(local,video=video,ext=ext)
    info=dict(id='abcdefghijk',description=text,like_count=0,extractor_key='Youtube',**extra)
    if isinstance(text,str):(stage/('audio.description'if video=='audio'else'video.description')).write_bytes(text.encode('utf-8'))
    result['details']=details.capture(info,stage,result['capture'],engine_version='fixture')
    jobs.publish(job,stage,result)
    return jobs.get(job['id'])


@pytest.mark.parametrize('number',[None,0,27,-1,False,True,1.5,'42',float('nan'),details.MAX_COUNT+1])
def test_only_supplied_integral_counts_not_missing_or_invalid_zero(tmp_path,number):
    info=dict(id='abcdefghijk',like_count=number,dislike_count=number)
    value=details.capture(info,tmp_path,facts(),engine_version='fixture')
    expected=number if type(number)is int and 0<=number<=details.MAX_COUNT else None
    assert value['like_count']==expected and value['dislike_count']==expected
    assert value['description']is None and value['description_status']=='absent'


@pytest.mark.parametrize('text',['','한국어\n日本語 😀\r\n  whitespace stays  ','<script>alert(1)</script>\nhttps://invalid.test/image'])
def test_complete_engine_bytes_and_immutable_hash_asset(tmp_path,text):
    value=snapshot(tmp_path,text)
    raw=details.asset_bytes(tmp_path,value)
    assert raw==text.encode('utf-8')
    assert value['description']['sha256']==hashlib.sha256(raw).hexdigest()
    assert details.capture(dict(id='abcdefghijk',description=text,like_count=0),tmp_path,
        dict(facts(),captured_at=value['captured_at']),engine_version='fixture')['description']==value['description']


def test_description_limits_and_engine_mismatch_never_silently_shorten(tmp_path):
    value=snapshot(tmp_path,'x'*(details.MAX_BYTES+1))
    assert value['description']is None and value['description_status']=='unavailable'
    assert (tmp_path/'video.description').stat().st_size==details.MAX_BYTES+1
    (tmp_path/'video.description').write_bytes(b'not the supplied text')
    value=details.capture(dict(id='abcdefghijk',description='complete supplied text'),tmp_path,facts(),engine_version='fixture')
    assert value['description']is None and (tmp_path/'video.description').read_bytes()==b'not the supplied text'


def test_new_snapshots_keep_all_earlier_wording_and_reject_forged_descriptor(tmp_path):
    first=snapshot(tmp_path,'First wording');first_bytes=details.asset_bytes(tmp_path,first)
    second=snapshot(tmp_path,'Changed wording')
    assert first['description']['path']!=second['description']['path']
    assert details.asset_bytes(tmp_path,first)==first_bytes and details.asset_bytes(tmp_path,second)==b'Changed wording'
    for change in [dict(like_count=False),dict(video_id='different'),dict(extractor='https://signed.invalid/token'),dict(description_status='absent')]:
        with pytest.raises(ValueError):details.validate({**first,**change},facts(captured_at=first['captured_at']))
    malformed=copy.deepcopy(first);malformed['description']['path']='../outside.txt'
    with pytest.raises(ValueError):details.validate(malformed,facts(captured_at=first['captured_at']))


def test_saved_details_and_export_use_no_engine_and_keep_job_payload_small(local,monkeypatch):
    source,root,connect=local;body='한국어 description\n'+('long line\n'*2000);job=rich_save(local,body,dislike_count=7)
    monkeypatch.setattr(router,'_playlist_enabled',lambda:None)
    monkeypatch.setattr(jobs,'dependencies',lambda:pytest.fail('Offline reading requested acquisition tools'))
    response=Response();value=router.saved_details(job['id'],response)
    assert value['description']==body and value['like_count']==0 and value['dislike_count']==7
    assert response.headers['cache-control']=='private, no-store'
    exported=router.saved_description(job['id']);assert exported.body==body.encode('utf-8')
    assert 'A%20saved%20song.description.txt'in exported.headers['content-disposition']
    assert exported.headers['x-content-type-options']=='nosniff'and exported.media_type=='text/plain'
    assert 'description'not in job and body not in json.dumps(job)
    with connect()as db:
        captured=metadata.preserved_media(db,job['id'],source_id=source.source_id,source_root=root,forbidden_roots=())
    assert captured['assets']['metadata']['sha256']==hashlib.sha256(body.encode()).hexdigest()
    assert (root/job['path']).read_bytes()==b'preserved completed media'


@pytest.mark.parametrize('text',[None,''])
def test_absent_and_empty_description_are_different_and_old_saves_stay_readable(local,monkeypatch,text):
    job=rich_save(local,text);monkeypatch.setattr(router,'_playlist_enabled',lambda:None)
    value=router.saved_details(job['id'],Response())
    assert value['description_available']==(text=='')
    if text=='':assert router.saved_description(job['id']).body==b''
    else:
        with pytest.raises(HTTPException)as error:router.saved_description(job['id'])
        assert error.value.status_code==404
    old_job,stage,result=staged(local);jobs.publish(old_job,stage,result)
    old=router.saved_details(old_job['id'],Response());assert not old['captured']and old['like_count']is None and old['description']is None


def test_optional_description_loss_or_tamper_keeps_media_and_confirmed_counts(local,monkeypatch):
    _,root,_=local;job=rich_save(local);monkeypatch.setattr(router,'_playlist_enabled',lambda:None)
    value=item_manifest.decode((root/job['bundle_path']/item_manifest.MANIFEST).read_bytes())
    asset=value['extensions']['youtube']['details']['description'];path=root/job['bundle_path']/asset['path']
    original=path.read_bytes();path.write_bytes(b'changed bytes')
    assert jobs.published_output(job,root)['path']==job['path']
    result=router.saved_details(job['id'],Response());assert result['like_count']==0 and result['description_status']=='unavailable'
    with pytest.raises(HTTPException):router.saved_description(job['id'])
    path.unlink();assert jobs.published_output(job,root)['path']==job['path']
    assert not router.saved_details(job['id'],Response())['description_available']
    path.write_bytes(original);assert router.saved_description(job['id']).body==original


def test_hidden_nested_and_changed_sources_refuse_details_without_changes(local,monkeypatch):
    source,root,connect=local;job=rich_save(local);monkeypatch.setattr(router,'_playlist_enabled',lambda:None)
    from files_base import sources
    with connect()as db:db.execute('UPDATE files_sources SET visible=0 WHERE source_id=?',(source.source_id,))
    with pytest.raises(HTTPException)as error:router.saved_details(job['id'],Response())
    assert error.value.status_code==404
    with connect()as db:db.execute('UPDATE files_sources SET visible=1 WHERE source_id=?',(source.source_id,))
    with connect()as db:sources.register_source(db,root/job['bundle_path'])
    with pytest.raises(HTTPException):router.saved_description(job['id'])
    assert (root/job['path']).read_bytes()==b'preserved completed media'


def test_portable_refresh_archives_manifest_and_keeps_old_description_bytes(local):
    _,root,_=local;job=rich_save(local,'First wording');directory=root/job['bundle_path']
    original=(directory/item_manifest.MANIFEST).read_bytes();old=item_manifest.decode(original)
    new=copy.deepcopy(old);before=new['extensions']['youtube']['details'];new['extensions']['youtube']['details']=snapshot(directory,'Later wording')
    later=new['extensions']['youtube']['details'];later.update(captured_at=before['captured_at'])
    new['assets']=[a for a in new['assets']if a['role']!='metadata']+[item_manifest.asset(later['description']['path'],role='metadata',size=later['description']['size'],sha256=later['description']['sha256'],media_type='text/plain',required=False)]
    item_manifest.write(directory,new,refresh=True)
    assert (directory/'.keivotos/history'/ (hashlib.sha256(original).hexdigest()+'.json')).read_bytes()==original
    assert details.asset_bytes(directory,before)==b'First wording'and details.asset_bytes(directory,later)==b'Later wording'


def test_real_asgi_routes_serialize_zero_and_enforce_bounds_and_owner(local,monkeypatch):
    import asyncio
    from fastapi import FastAPI
    job=rich_save(local,'HTTP 한국어 text',video='audio',ext='m4a',dislike_count=9)
    monkeypatch.setattr(router,'_playlist_enabled',lambda:None)
    application=FastAPI();application.include_router(router.router)
    async def request(path):
        sent=[]
        async def receive():return{'type':'http.request','body':b'','more_body':False}
        async def send(message):sent.append(message)
        await application({'type':'http','http_version':'1.1','asgi':{'version':'3.0'},'method':'GET','path':path,
            'raw_path':path.encode(),'query_string':b'','headers':[],'scheme':'http','server':('localhost',80),'client':('test',1),'root_path':''},receive,send)
        return sent[0]['status'],dict(sent[0]['headers']),b''.join(m.get('body',b'')for m in sent[1:])
    status,headers,raw=asyncio.run(request('/api/youtube/videos/'+job['id']+'/details'))
    assert status==200 and json.loads(raw)['like_count']==0 and json.loads(raw)['dislike_count']==9
    status,headers,raw=asyncio.run(request('/api/youtube/videos/'+job['id']+'/description'))
    assert status==200 and raw=='HTTP 한국어 text'.encode()and headers[b'content-type']==b'text/plain; charset=utf-8'
    assert asyncio.run(request('/api/youtube/videos/'+'a'*65+'/details'))[0]==422
    monkeypatch.setattr(router,'_playlist_enabled',lambda:(_ for _ in()).throw(HTTPException(409,'Module disabled')))
    assert asyncio.run(request('/api/youtube/videos/'+job['id']+'/details'))[0]==409


def test_description_link_refuses_foreign_bytes_and_preserves_media(local,monkeypatch,tmp_path):
    _,root,_=local;job=rich_save(local);monkeypatch.setattr(router,'_playlist_enabled',lambda:None)
    manifest=item_manifest.decode((root/job['bundle_path']/item_manifest.MANIFEST).read_bytes())
    path=root/job['bundle_path']/manifest['extensions']['youtube']['details']['description']['path']
    retained=path.read_bytes();foreign=tmp_path/'foreign.txt';foreign.write_bytes(b'foreign private text');path.unlink()
    try:path.symlink_to(foreign)
    except OSError as error:
        path.write_bytes(retained)
        if getattr(error,'winerror',None)==1314:pytest.skip('Windows symbolic-link privilege unavailable')
        raise
    with pytest.raises(HTTPException):router.saved_description(job['id'])
    assert foreign.read_bytes()==b'foreign private text'and(root/job['path']).read_bytes()==b'preserved completed media'

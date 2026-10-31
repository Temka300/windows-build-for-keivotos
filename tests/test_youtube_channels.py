"""Local channel navigation over real publication/catalog fixtures, without network."""
import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

from tests.test_youtube_metadata import local, publish, facts, CID
from modules.youtube import channels, jobs, router
from files_base import sources

OTHER = 'UC'+'b'*22


def listing(local, query='', offset=0, limit=60):
    return channels.listing(local[2], query, offset, limit)


def test_empty_and_accepted_request_group_without_guessing_identity(local):
    assert listing(local)=={'items':[],'total':0}
    source,_,_=local
    job=jobs.create('https://www.youtube.com/watch?v=abcdefghijk',source.source_id)
    result=listing(local)
    assert result['items']==[dict(id='unknown',name='Unknown channel',avatar_id=None,item_count=1)]
    assert channels.videos(local[2],'unknown','',0,60)['items'][0]['id']==job['id']
    jobs.cancel(job['id'])
    assert listing(local)['total']==0


def test_stable_id_combines_renames_but_not_identical_names(local):
    first=publish(local,facts(channel_name='Old name',captured_at='2026-10-01T00:00:00Z'))
    latest=publish(local,facts(channel_name='Same name',captured_at='2026-10-02T00:00:00Z'))
    other=publish(local,facts(channel_id=OTHER,channel_url='https://www.youtube.com/channel/'+OTHER,channel_name='Same name'))
    rows={row['id']:row for row in listing(local)['items']}
    assert set(rows)=={CID,OTHER} and rows[CID]['name']==rows[OTHER]['name']=='Same name'
    assert rows[CID]['item_count']==2 and rows[OTHER]['item_count']==1
    assert {r['id']for r in channels.videos(local[2],CID,'',0,60)['items']}=={first['id'],latest['id']}
    assert jobs.get(first['id'])['channel_name']=='Old name'
    assert jobs.get(other['id'])['channel_id']==OTHER


def test_local_avatar_selection_missing_art_and_neutral_unknown(local):
    first=publish(local)
    missing=publish(local,facts(captured_at='2027-01-01T00:00:00Z'))
    assert listing(local)['items'][0]['avatar_id']==first['id']
    unknown=jobs.create('https://www.youtube.com/watch?v=abcdefghijk',local[0].source_id)
    jobs.update(unknown['id'],channel_name='Untrusted display name')
    neutral=next(r for r in listing(local)['items']if r['id']=='unknown')
    assert neutral['name']=='Unknown channel' and neutral['avatar_id'] is None
    assert jobs.get(missing['id'])['avatar']==''


def test_literal_unicode_name_and_title_search_stays_scoped(local):
    saved=publish(local,facts(channel_name='Straße 한국어'))
    other=publish(local,facts(channel_id=OTHER,channel_url='https://www.youtube.com/channel/'+OTHER,channel_name='Other'))
    jobs.update(saved['id'],title='한국어 100%_Saved')
    assert [r['id']for r in listing(local,'STRASSE')['items']]==[CID]
    assert [r['id']for r in listing(local,'100%_')['items']]==[CID]
    assert channels.videos(local[2],CID,'한국어',0,60)['total']==1
    assert channels.videos(local[2],OTHER,'한국어',0,60)['total']==0
    assert channels.videos(local[2],CID,'100%x',0,60)['total']==0
    assert jobs.get(other['id'])['title']=='Saved'


@pytest.mark.parametrize('change',['hidden','role','root'])
def test_changed_source_cannot_contribute_channels_or_rows(local,change,tmp_path):
    saved=publish(local)
    assert listing(local)['total']==1
    with local[2]() as db:
        column,value={'hidden':('visible',0),'role':('role','manga'),'root':('path',str(tmp_path/'changed'))}[change]
        db.execute('UPDATE files_sources SET '+column+'=? WHERE source_id=?',(value,local[0].source_id));db.commit()
    assert listing(local)['total']==0
    with pytest.raises(ValueError):channels.videos(local[2],CID,'',0,60)
    assert (local[1]/saved['path']).read_bytes()==b'preserved video'


def test_catalog_only_channel_after_history_loss_keeps_original_bytes(local):
    saved=publish(local);directory=local[1]/saved['bundle_path']
    before={str(p.relative_to(directory)):p.read_bytes()for p in directory.rglob('*')if p.is_file()}
    with jobs.get_jobs_db() as db:
        db.execute('DELETE FROM youtube_downloads WHERE id=?',(saved['id'],));db.commit()
    assert listing(local)['items'][0]['id']==CID
    assert channels.videos(local[2],CID,'',0,60)['items'][0]['id']==saved['id']
    assert before=={str(p.relative_to(directory)):p.read_bytes()for p in directory.rglob('*')if p.is_file()}


def test_cancelled_and_replaced_failures_never_change_channel_totals(local):
    source,_,_=local
    failed=jobs.create('https://www.youtube.com/watch?v=abcdefghijk',source.source_id)
    jobs.update(failed['id'],status='failed',error='local fixture')
    replacement=jobs.retry(failed['id'])
    page=channels.videos(local[2],'unknown','',0,60)
    assert page['total']==1 and page['items'][0]['id']==replacement['id']
    jobs.cancel(replacement['id'])
    assert listing(local)['total']==0


def test_channel_pages_are_bounded_distinct_and_search_all_saved_names(local):
    saved=publish(local)
    with jobs.get_jobs_db()as db:
        template=dict(db.execute('SELECT * FROM youtube_downloads WHERE id=?',(saved['id'],)).fetchone())
        for index in range(74):
            identity='UC'+str(index).zfill(22)
            row={**template,'id':f'channel-{index:03}','channel_id':identity,
                 'channel_url':'https://www.youtube.com/channel/'+identity,'channel_name':f'한글 100%_{index:03}',
                 'status':'failed','path':'','avatar':'','error':'retained fixture'}
            db.execute('INSERT INTO youtube_downloads('+','.join(row)+')VALUES('+','.join('?'for _ in row)+')',tuple(row.values()))
        db.commit()
    first,last=listing(local,limit=60),listing(local,offset=60,limit=60)
    assert first['total']==last['total']==75 and len(first['items'])==60 and len(last['items'])==15
    assert len({row['id']for row in first['items']+last['items']})==75
    assert listing(local,'100%_')['total']==74 and listing(local,'100x')['total']==0


def test_later_pages_and_random_offsets_use_same_channel_search(local,monkeypatch):
    saved=publish(local);other=publish(local,facts(channel_id=OTHER,channel_url='https://www.youtube.com/channel/'+OTHER))
    with jobs.get_jobs_db() as db:
        template=dict(db.execute('SELECT * FROM youtube_downloads WHERE id=?',(saved['id'],)).fetchone())
        for index in range(130):
            row={**template,'id':f'queued-{index:03}','status':'queued','path':'','avatar':'','title':f'Later {index:03}','created_at':'9999-01-01T00:00:00Z'}
            db.execute('INSERT INTO youtube_downloads('+','.join(row)+')VALUES('+','.join('?'for _ in row)+')',tuple(row.values()))
        db.commit()
    first=channels.videos(local[2],CID,'',0,120);last=channels.videos(local[2],CID,'',120,120)
    assert first['total']==131 and len(first['items'])==120 and len(last['items'])==11
    assert len({r['id']for r in first['items']+last['items']})==131
    assert other['id']not in {r['id']for r in first['items']+last['items']}
    monkeypatch.setattr(router,'_playlist_enabled',lambda:None)
    chosen=router.channel_random(CID,Response(),q='',current_id='')
    assert chosen['item']['id']==saved['id'] and chosen['offset']==130 and chosen['total']==131
    assert router.channel_random(CID,Response(),q='Later',current_id='')['item']is None


@pytest.mark.parametrize('unsafe',['missing','child','link','audio'])
def test_scoped_random_refuses_unavailable_foreign_or_audio_media(local,monkeypatch,tmp_path,unsafe):
    from tests.test_youtube_details import rich_save
    saved=rich_save(local,video='audio',ext='m4a')if unsafe=='audio'else publish(local)
    path=local[1]/saved['path'];monkeypatch.setattr(router,'_playlist_enabled',lambda:None)
    if unsafe=='missing':path.rename(path.with_suffix('.retained'))
    elif unsafe=='child':
        with local[2]()as db:sources.register_source(db,path.parent);db.commit()
    elif unsafe=='audio':pass
    else:
        target=tmp_path/'foreign.mp4';target.write_bytes(b'foreign');path.rename(path.with_suffix('.retained'))
        try:path.symlink_to(target)
        except OSError as error:
            if getattr(error,'winerror',None)==1314:pytest.skip('Windows symbolic-link privilege unavailable')
            raise
    if unsafe=='child':
        assert listing(local)['total']==0
        with pytest.raises(HTTPException)as error:router.channel_random(CID,Response(),q='',current_id='')
        assert error.value.status_code==404
    else:assert router.channel_random(CID,Response(),q='',current_id='')['item']is None


def test_actual_api_bounds_owner_and_legacy_library_contract(local,monkeypatch):
    saved=publish(local);monkeypatch.setattr(router,'_playlist_enabled',lambda:None)
    app=FastAPI();app.include_router(router.router)
    async def request(path,query=b''):
        sent=[]
        async def receive():return dict(type='http.request',body=b'',more_body=False)
        async def send(value):sent.append(value)
        await app(dict(type='http',http_version='1.1',asgi={'version':'3.0'},method='GET',path=path,raw_path=path.encode(),query_string=query,headers=[],scheme='http',server=('localhost',80),client=('test',1),root_path=''),receive,send)
        return sent[0]['status'],b''.join(m.get('body',b'')for m in sent[1:])
    status,raw=asyncio.run(request('/api/youtube/channels'));assert status==200 and json.loads(raw)['items'][0]['id']==CID
    assert asyncio.run(request('/api/youtube/channels',b'limit=121'))[0]==422
    assert asyncio.run(request('/api/youtube/channels/not-a-channel/videos'))[0]==422
    assert asyncio.run(request('/api/youtube/channels/'+OTHER+'/videos'))[0]==404
    assert asyncio.run(request('/api/youtube/channels/'+CID+'/videos',b'offset=-1'))[0]==422
    legacy=router.library(q='',offset=0,limit=60);assert legacy['items'][0]['id']==saved['id'] and 'channel_key'not in legacy['items'][0] and 'root'not in legacy['items'][0]
    monkeypatch.setattr(router,'_playlist_enabled',lambda:(_ for _ in()).throw(HTTPException(409,'Module disabled')))
    assert asyncio.run(request('/api/youtube/channels'))[0]==409
    assert asyncio.run(request('/api/youtube/channels/'+CID+'/random'))[0]==409

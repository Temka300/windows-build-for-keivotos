"""Test-only local media/history and real controlled child downloads."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
from PIL import Image

WORKER=r'''
import json,sys,time
from pathlib import Path
from PIL import Image
spec=json.loads(sys.stdin.readline())
folder=Path(spec['directory'])
payload=Path(sys.argv[1]).read_bytes()
title='Fixture YouTube title '+spec['id'][:6]
time.sleep(1.8)
print(json.dumps({'event':'metadata','title':title}),flush=True)
time.sleep(.7)
Image.new('RGB',(96,64),'#8b74b2').save(folder/'video.jpg')
(folder/'metadata.json').write_bytes(b'preserved fixture metadata')
names=['video.f137.mp4.part','video.f140.m4a.part'] if spec['video']=='720' else ['video.mp4.part']
release=Path(spec['root'])/('release-'+spec['id'])
tick=0
while not release.exists():
 for name in names:
  with (folder/name).open('ab') as output:output.write(b'p')
 print(json.dumps({'event':'file','name':title,
   'progress':min(90,10+tick),'files':names,'artwork':'video.jpg'}),flush=True)
 tick+=1
 if tick>3000:raise SystemExit(3)
 time.sleep(.08)
first=folder/names[0]
first.write_bytes(payload)
first.rename(folder/'video.mp4')
for name in names[1:]:(folder/name).unlink()
print(json.dumps({'event':'file','name':title,'progress':99,'files':['video.mp4'],'artwork':'video.jpg'}),flush=True)
print(json.dumps({'event':'complete','path':'video.mp4','title':title,'thumbnail':'video.jpg'}),flush=True)
'''


def seed(media):
    from browser_video_fixture import seed_videos
    media.mkdir()
    seed_videos(media.parent/'fixture-video')
    notes=media.parent/'notes';notes.mkdir()
    (notes/'Welcome.md').write_text('# Local note\n',encoding='utf-8')


def install(media):
    from fastapi import Request
    from fastapi.routing import APIRoute
    from server import app
    from modules.youtube import jobs,router
    from database import get_user_db,get_jobs_db
    from files_base import sources
    from services import operations,download_history as history,download_controls as controls
    jobs.dependencies=lambda:{'ready':True,'missing':[],'ffmpeg':'fixture'}
    router.dependencies=jobs.dependencies
    original=asyncio.create_subprocess_exec
    async def spawn(*command,**options):
        if '--youtube-worker' in command:
            return await original(sys.executable,'-I','-c',WORKER,str(media.parent/'fixture-video/sample.mp4'),**options)
        return await original(*command,**options)
    asyncio.create_subprocess_exec=spawn
    state={'jobs':[],'seeded':False,'counter':0}

    async def start(request:Request):
        body=await request.json()
        state['counter']+=1
        with get_user_db() as db:
            source=next(s for s in sources.list_sources(db) if Path(s.path)==media)
        job=jobs.create('https://www.youtube.com/watch?v='+str(state['counter']).zfill(11),
            source.source_id,video='720' if body.get('multipart') else 'best')
        state['jobs'].append({'id':job['id'],'multipart':bool(body.get('multipart'))})
        return {'job_id':job['id'],'item_id':history.identity('youtube',job['id'],'media')}

    async def finish(request:Request):
        identity=(await request.json())['job_id']
        if not any(row['id']==identity for row in state['jobs']):
            job=jobs.get(identity)
            if not job or Path(job['root'])!=media:raise ValueError('Unknown fixture job')
            state['jobs'].append({'id':identity,'multipart':job['video']=='720'})
        (media/('release-'+identity)).write_text('finish')
        return {'ok':True}

    def proof():
        result={}
        for row in state['jobs']:
            job=jobs.get(row['id'])
            stage=media/'.keivotos/youtube'/row['id']
            names=['video.f137.mp4.part','video.f140.m4a.part'] if row['multipart'] else ['video.mp4.part']
            metadata=stage/'metadata.json'
            result[row['id']]={
                'parts':{name:(stage/name).stat().st_size if (stage/name).exists() else None for name in names},
                'metadata':hashlib.sha256(metadata.read_bytes()).hexdigest() if metadata.is_file() else None,
                'status':job['status'],
                'published':bool(job['path'] and (media/job['path']).is_file())}
        with get_jobs_db() as db:
            legacy=history._one(db,"SELECT name FROM kv_download_items WHERE operation_id='fixture-history-1'")
        result['_legacy']=legacy
        result['_notes']=(media.parent/'notes/Welcome.md').read_text()
        return result

    async def seed_history(request:Request):
        count=min(600,max(1,int((await request.json()).get('count',9))))
        if state['seeded']:return {'ok':True}
        state['seeded']=True
        roots={}
        with get_user_db() as db:
            for owner in ('danbooru','youtube','twitter'):
                folder=media.parent/('history-'+owner);folder.mkdir()
                source=sources.register_source(db,folder,'History '+owner)
                sources.update_source(db,source.source_id,role=owner)
                roots[owner]=(folder,source.source_id)
            db.commit()
        with get_jobs_db() as db:
            for number in range(count):
                owner=('danbooru','youtube','twitter')[number%3]
                folder,source_id=roots[owner]
                name=('An intentionally long filename '+('韓글 source name '*6) if number==0 else 'Saved '+str(number).zfill(3))+'.png'
                Image.new('RGB',(64,48),('#8b74b2','#5b8fa3','#af879a')[number%3]).save(folder/name)
                identity='fixture-history-'+str(number)
                operations.request(db,owner,identity,kind='download',key=identity,payload={})
                operations.claim(db,owner,identity)
                item=history.record(db,owner,identity,file_key='media',name='video.webm' if number==1 else name,module_name={'danbooru':'Danbooru','youtube':'YouTube','twitter':'Twitter'}[owner],
                    completed=True,at=('2026-10-26' if number%2==0 else '2026-10-25')+'T06:00:00Z')
                if number==1:
                    db.execute("INSERT INTO youtube_downloads(id,url,source_id,root,title,status,path,thumbnail,video,audio) VALUES(?,?,?,?,?,'complete',?,?,'best','best')",
                        (identity,'https://www.youtube.com/watch?v=yyyyyyyyyyy',source_id,str(folder),'Saved YouTube fixture title',name,name))
                controls.bind_target(db,item,source_id=source_id,root=folder,source_role=owner,paths=[name],artwork=name,published=True)
                operations.transition(db,owner,identity,state='completed')
            db.execute("UPDATE kv_download_items SET completed_at=NULL WHERE operation_id=?",(identity,))
            db.commit()
        return {'ok':True,'count':count}

    def language():
        with get_user_db() as db:
            source=sources.register_source(db,media.parent/'notes','Notes')
            sources.update_source(db,source.source_id,role='language')
            db.commit()
        return {'ok':True}

    for route,callback,methods in (
        ('start',start,['POST']),('finish',finish,['POST']),('proof',proof,['GET']),
        ('seed',seed_history,['POST']),('language',language,['POST'])):
        app.router.routes.insert(0,APIRoute('/api/fixture/downloads/'+route,callback,methods=methods))

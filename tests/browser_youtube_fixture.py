"""Test-only local download simulator: real queue/publication, no remote traffic."""
import asyncio
import logging
from pathlib import Path
import shutil
import uuid
from PIL import Image, ImageDraw


def seed(media):
    from browser_video_fixture import seed_videos
    media.mkdir()
    (media.parent/'custom-downloads').mkdir()
    seed_videos(media.parent/'fixture-video')
    image=Image.new('RGB',(640,360),'#687e8b');draw=ImageDraw.Draw(image)
    draw.ellipse((460,40,540,120),fill='#e5d1ae')
    draw.polygon([(0,340),(160,130),(330,360),(490,190),(640,350)],fill='#344851')
    image.save(media.parent/'fixture-cover.png')
    Image.new('RGB',(40,40),'#d094b2').save(media.parent/'fixture-avatar.png')


def install(media):
    from modules.youtube import jobs
    from modules.youtube import router
    tools=lambda:{'ready':True,'missing':[],'ffmpeg':'fixture'}
    jobs.dependencies=tools;router.dependencies=tools
    class ExpectedFailures(logging.Filter):
        def filter(self,record):
            if record.exc_info and str(record.exc_info[1]) in ('fixture media acquisition failure','fixture annotation failure after completed publication'):
                record.msg+='; injected failure: %s';record.args=(*record.args,str(record.exc_info[1]))
                record.exc_info=None;record.exc_text=None
                record.levelno=logging.WARNING;record.levelname='WARNING'
            return True
    jobs.logger.addFilter(ExpectedFailures())
    attempts={};registration_failures=set()
    original_annotation=jobs.annotations.upsert_annotation
    def annotate(*args,**kwargs):
        relative=kwargs.get('relative_path')
        failed=next((identity for identity in registration_failures if relative and relative.endswith(f' [{identity[:16]}]/video.mp4')),None)
        if failed:
            registration_failures.remove(failed)
            raise RuntimeError('fixture annotation failure after completed publication')
        return original_annotation(*args,**kwargs)
    jobs.annotations.upsert_annotation=annotate
    def playlist_fixture(job):
        # Disposable local-only corpus: 131 ordered members, missing media and
        # a retained missing-job reference. Original fixture bytes are copied.
        from database import get_user_db
        from modules.youtube.user_schema import import_catalog_library
        source,root=jobs.destination(job['source_id'],job['root'])
        published=jobs.get(job['id'])
        refs=[job['id']]
        with get_user_db() as connection:
            original=jobs.get(job['id'])
            for index in range(1,131):
                identity=uuid.uuid5(uuid.NAMESPACE_OID,job['id']+':playlist:'+str(index)).hex
                refs.append(identity)
                if index==129:continue
                relative=f'YouTube/{identity}/Video {index:03}.mp4'
                directory=root/'YouTube'/identity;directory.mkdir()
                if index not in (1,130):shutil.copy2(media.parent/'fixture-video/sample.mp4',directory/f'Video {index:03}.mp4')
                shutil.copy2(media.parent/'fixture-cover.png',directory/'cover.png')
                row={**original,'id':identity,'title':f'Playlist video {index:03}','path':relative,'thumbnail':f'YouTube/{identity}/cover.png',
                     'layout_version':0,'bundle_path':'','manifest_sha256':''}
                if original['avatar']:
                    private=directory/'.keivotos';private.mkdir()
                    shutil.copy2(root/original['avatar'],private/'channel-avatar.png')
                    row['avatar']=f'YouTube/{identity}/.keivotos/channel-avatar.png'
                columns=list(row)
                # Generated historical saves exercise the existing catalog bridge;
                # native owner rows live in the operations file after item 03.
                statement='INSERT INTO youtube_downloads ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')'
                connection.execute(statement,tuple(row[name] for name in columns))
                with jobs.get_jobs_db() as operational:
                    operational.execute(statement,tuple(row[name] for name in columns))
                    operational.commit()
            import_catalog_library(connection)
            for title,positions in [('Ordered queue fixture',range(131)),('Single video fixture',[2]),('Unavailable queue fixture',[1,129,130])]:
                identity=str(uuid.uuid5(uuid.NAMESPACE_OID,job['id']+':'+title))
                connection.execute('INSERT INTO youtube_playlists (id,request_id,title) VALUES (?,?,?)',(identity,str(uuid.uuid4()),title))
                for position in positions:
                    connection.execute('INSERT INTO youtube_playlist_items (playlist_id,job_id,position) VALUES (?,?,?)',(identity,refs[position],position))
            connection.commit()
        return published

    async def execute(job):
        _,root=jobs.destination(job['source_id'],job['root'])
        stage=jobs.child_directory(root,['.keivotos','youtube'])/job['id'];stage.mkdir()
        special=job['url'].rsplit('=',1)[-1]
        attempts[special]=attempts.get(special,0)+1
        title={'retryfailed':'Retry failure fixture','repairvideo':'Registration recovery fixture','stalefailed':'Pagination fixture retry','playlistfix':'Playlist video 000','avatarblank':'No channel image','avatarchang':'Renamed channel video','avatardupes':'Same name channel video','channelpair':'Channel navigation fixture'}.get(special,'A quiet journey through the valley')
        if special=='retryfailed' and attempts[special]<=2 or special=='stalefailed' and attempts[special]==1:
            jobs.update(job['id'],title=title)
            (stage/'unfinished.part').write_bytes(b'preserved failed attempt')
            await asyncio.sleep(.4)
            raise RuntimeError('fixture media acquisition failure')
        for progress in [10,25,45,65,90]:
            if jobs.get(job['id'])['status']=='cancelled':return
            jobs.update(job['id'],progress=progress,title=title)
            await asyncio.sleep(.7)
        shutil.copy2(media.parent/'fixture-video/sample.mp4',stage/'Valley.mp4')
        shutil.copy2(media.parent/'fixture-cover.png',stage/'cover.png')
        from modules.youtube import metadata, details
        capture=metadata.capture(dict(id=special,channel_id='UC'+('b' if special=='avatardupes' else 'c' if special=='channelpair' else 'a')*22,
            channel='Renamed channel' if special in ('avatarchang','channelpair') else 'Local channel',duration=2.5,upload_date='20261010',format_id='fixture-mp4',vcodec='h264',acodec='aac'),job['url'],'Valley.mp4')
        if special!='avatarblank':
            (stage/'.keivotos').mkdir();shutil.copy2(media.parent/'fixture-avatar.png',stage/'.keivotos/channel-avatar.png')
            capture.update(avatar='.keivotos/channel-avatar.png',avatar_error='')
        snapshot=None
        if special!='avatarblank':
            body='한국어 description\nFull saved wording. <img src="https://invalid.test/not-an-image">\n'+('A long local description line.\n'*100)
            (stage/'video.description').write_bytes(body.encode('utf-8'))
            snapshot=details.capture(dict(id=special,description=body,like_count=0,
                dislike_count=7 if special=='avatardupes' else None,extractor_key='Youtube'),stage,capture,engine_version='fixture')
        if special=='repairvideo':registration_failures.add(job['id'])
        completion=asyncio.create_task(asyncio.to_thread(jobs.publish,job,stage,{'path':'Valley.mp4','thumbnail':'cover.png','title':title,'capture':capture,'details':snapshot}))
        try:await asyncio.shield(completion)
        except asyncio.CancelledError:await completion;raise
        if special=='playlistfix':await asyncio.to_thread(playlist_fixture,job)
    jobs.execute=execute

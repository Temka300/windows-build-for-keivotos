"""Tiny synthetic codec fixtures, generated locally with the existing FFmpeg."""
from pathlib import Path
import subprocess
import imageio_ffmpeg


def seed_videos(media: Path):
    media.mkdir()
    for name, codec in [('sample.mp4', 'libx264'), ('sample.webm', 'libvpx')]:
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
                        '-f', 'lavfi', '-i', 'testsrc=size=320x180:rate=12', '-t', '4',
                        '-c:v', codec, '-pix_fmt', 'yuv420p', '-an', str(media / name)], check=True)
    (media / 'sample.m4v').write_bytes((media / 'sample.mp4').read_bytes())
    (media / 'unsupported.avi').write_bytes(b'unsupported fixture')
    (media / 'broken.mp4').write_bytes(b'invalid media fixture')
    (media / 'notes.txt').write_text('not a video')
    paged = media.parent / 'category-pages'
    paged.mkdir()
    for number in range(125):
        (paged / f'clip{number:03}.mp4').write_bytes((media / 'sample.mp4').read_bytes())

    # Registered only in the series-browsing section of the browser test, so
    # existing flat-player queue regressions keep their original fixture order.
    series = media.parent / 'series-library'
    for relative in ['Order/Season 1/Episode 10.mp4', 'Order/Season 1/Episode 2.mp4',
                     'Order/Season 1/Episode 1.mp4', 'Order/Season 2/Episode 1.mp4',
                     'Order/Season 10/Episode 1.mp4', 'Order/Specials/Extra.mp4',
                     'A very long standalone title that stays visible and accessible.mp4']:
        target=series/relative;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes((media/'sample.mp4').read_bytes())
    for number in range(1,126):
        target=series/'Show 2'/'Season 1'/f'Episode {number}.mp4'
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((media/'sample.mp4').read_bytes())
    for relative in ['Show 2/Season 2/Episode 1.mp4','Show 2/Season 10/Episode 1.mp4']:
        target=series/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((media/'sample.mp4').read_bytes())
    from PIL import Image
    Image.new('RGB',(300,450),'#655291').save(series/'Show 2'/'cover.jpg')
    Image.new('RGB',(300,450),'#429176').save(series/'Order'/'folder.jpg')

    watch = media.parent/'watch-library'/'Resume'
    watch.mkdir(parents=True)
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-hide_banner','-loglevel','error',
                    '-stream_loop','-1','-i',str(media/'sample.mp4'),'-t','20','-c','copy',str(watch/'Episode 1.mp4')],check=True)
    (watch/'Episode 2.mp4').write_bytes((watch/'Episode 1.mp4').read_bytes())

    # Independent metadata corpus. Existing flat-player and category fixtures keep their exact names/order.
    import json,hashlib
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
    from services import item_manifest as portable
    metadata=media.parent/'metadata-library'
    for relative in [*[f'Original Series/Episode {number}.mp4' for number in range(1,13)],'Standalone One.mp4','Standalone Two.mp4','Native Bundle/video.mp4']:
        target=metadata/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((watch/'Episode 1.mp4').read_bytes())
    (metadata/'Original Series/tvshow.nfo').write_text('<tvshow><title>NFO series</title><plot>NFO description</plot><director>Local director</director><genre>NFO genre</genre><tag>series tag</tag><year>2024</year><language>ja</language></tvshow>')
    (metadata/'Original Series/details.json').write_text(json.dumps({'title':'하늘 音楽','author':'Local author','description':'A locally stored anime series.','genre':['Study','Slice of life'],'status':'1','unknown':{'keep':['exact',42]}},ensure_ascii=False),encoding='utf-8')
    (metadata/'Original Series/Episode 1.nfo').write_text('<episodedetails><title>나의 영상</title><plot>Episode description</plot><tag>Korean</tag><tag>100%_</tag><watched>true</watched><playcount>8</playcount><lastplayed>yesterday</lastplayed></episodedetails>',encoding='utf-8')
    (metadata/'Standalone One.nfo').write_text('<movie><title>Local film</title><genre>Movie genre</genre><year>2023</year></movie>')
    (metadata/'movie.nfo').write_text('<movie><title>Ambiguous shared movie title</title></movie>')
    Image.new('RGB',(300,450),'#6781a5').save(metadata/'Original Series/cover.jpg')
    native=portable.new(item_id='native-fixture-video',owner='video',item_type='video',library_id='native-fixture-library',assets=[portable.asset('video.mp4',asset_id='native-fixture-media')],extensions={'video':{'title':'Native episode','tags':['native tag'],'unknown':{'original':True}}})
    portable.write(metadata/'Native Bundle',native)
    # Sibling season folders are separate titles; neither needs a section header.
    for folder,count in [('Separate Season 1',3),('Separate Season 2',2)]:
        for number in range(1,count+1):
            target=metadata/folder/f'Episode {number}.mp4';target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((watch/'Episode 1.mp4').read_bytes())
    (media.parent/'metadata-original-hashes.json').write_text(json.dumps({str(file.relative_to(metadata)):hashlib.sha256(file.read_bytes()).hexdigest() for file in metadata.rglob('*') if file.is_file()}))
    # Reuse the local-only YouTube simulator solely to exercise the real Downloads dirty-leave guard.
    fixture=media.parent/'fixture-video';fixture.mkdir(exist_ok=True);(fixture/'sample.mp4').write_bytes((media/'sample.mp4').read_bytes())
    Image.new('RGB',(640,360),'#687e8b').save(media.parent/'fixture-cover.png')
    Image.new('RGB',(40,40),'#d094b2').save(media.parent/'fixture-avatar.png')
    (media.parent/'metadata-downloads').mkdir()

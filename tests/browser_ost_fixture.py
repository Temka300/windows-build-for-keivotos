"""Generated audio and local artwork; no downloads, credentials or user media."""
from io import BytesIO
import math
from pathlib import Path
import struct
import subprocess
import wave

from PIL import Image, ImageDraw, ImageFont


def write_audio(path: Path, seconds=4):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        # A small generated tone is also suitable for later transport checks.
        period = b''.join(struct.pack('<h', int(2000 * math.sin(2 * math.pi * 440 * n / 8000)))
                          for n in range(8000))
        audio.writeframes(period * seconds)


def artwork(title, background='#263f59', accent='#83bedc'):
    image = Image.new('RGB', (400, 400), background)
    draw = ImageDraw.Draw(image)
    for offset in (0, 24, 48):
        draw.ellipse((90 + offset, 68 + offset, 370 - offset, 348 - offset), outline=accent, width=2)
    draw.ellipse((205, 182, 255, 232), fill=accent)
    for n in range(26):
        height = 14 + int(32 * abs(math.sin(n * .55)))
        draw.line((24 + n * 6, 275 - height, 24 + n * 6, 275 + height), fill=accent, width=2)
    try:
        font = ImageFont.truetype('DejaVuSans.ttf', 27)
        small = ImageFont.truetype('DejaVuSans.ttf', 13)
    except OSError:
        font = ImageFont.load_default(size=27)
        small = ImageFont.load_default(size=13)
    draw.text((22, 24), 'LOCAL OST', font=small, fill=accent)
    draw.text((22, 337), title, font=font, fill='#f5f2ea')
    data = BytesIO()
    image.save(data, 'PNG')
    return data.getvalue()


def embedded_audio(path: Path, picture: Path):
    import imageio_ffmpeg
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
                    '-f', 'lavfi', '-i', 'anullsrc=r=8000:cl=mono', '-i', str(picture),
                    '-map', '0:a', '-map', '1:v', '-c:a', 'flac', '-c:v', 'copy',
                    '-disposition:v', 'attached_pic', '-t', '4', str(path)], check=True, timeout=30)


def flac_comments(path: Path, comments: list[str]):
    """Replace only a generated fixture's Vorbis comment block, preserving frames/art."""
    data = path.read_bytes()
    assert data.startswith(b'fLaC')
    offset = 4
    blocks = []
    while True:
        header = data[offset:offset + 4]
        length = int.from_bytes(header[1:], 'big')
        content = data[offset + 4:offset + 4 + length]
        kind = header[0] & 127
        if kind == 4:
            vendor = b'Keivotos generated fixture'
            content = struct.pack('<I', len(vendor)) + vendor + struct.pack('<I', len(comments))
            for comment in comments:
                encoded = comment.encode('utf-8')
                content += struct.pack('<I', len(encoded)) + encoded
        blocks.append(bytes([kind | (header[0] & 128)]) + len(content).to_bytes(3, 'big') + content)
        offset += 4 + length
        if header[0] & 128:
            break
    path.write_bytes(b'fLaC' + b''.join(blocks) + data[offset:])


LONG_TRACK = 'A long soundtrack title with quiet rain and a distant train that wraps across two rows while its complete name remains readable 曲 2.wav'


def seed_ost(media: Path):
    media.mkdir()
    albums = ('Album 2', 'Album 10', 'Corrupt art', 'Empty', 'Large library', 'No art')
    for name in albums:
        (media / name).mkdir()
    (media / 'Album 2/Cover.JPG').write_bytes(artwork('BLUE HOURS'))
    (media / 'Album 2/folder.png').write_bytes(artwork('SECOND', '#32523f', '#a3d4ad'))
    (media / 'Album 2/front.webp').write_bytes(artwork('THIRD', '#553a53', '#d2a8d0'))
    embedded = media / 'Album 2/embedded-picture.png'
    embedded.write_bytes(artwork('FIRST LIGHT', '#725034', '#edc68a'))
    embedded_audio(media / 'Album 2/00 - Opening.flac', embedded)
    flac_comments(media / 'Album 2/00 - Opening.flac', [
        'TITLE=  새벽 / First light  ', 'ARTIST=Artist one', 'ARTIST=歌手 二',
        'ALBUM=Blue hours', 'ALBUMARTIST=Album artist', 'TRACKNUMBER=1',
        'DISCNUMBER=2', 'GENRE=Ambient', 'COMPOSER=作曲家', 'COMMENT=<img src="https://example.invalid/never">'])
    for name in ('Track 2.wav', 'Track 10.wav', LONG_TRACK):
        write_audio(media / 'Album 2' / name, seconds=20)
    write_audio(media / 'Album 2/Deep/曲 2.wav')
    (media / 'Album 10/folder.PNG').write_bytes(artwork('TEN', '#725034', '#edc68a'))
    write_audio(media / 'Album 10/Track 2.wav')
    (media / 'Corrupt art/cover.jpg').write_bytes(b'generated corrupt artwork')
    (media / 'Corrupt art/folder.png').write_bytes(artwork('RECOVERY', '#553a53', '#d2a8d0'))
    write_audio(media / 'Corrupt art/Track 2.wav')
    write_audio(media / 'No art/Track 2.wav')
    (media / 'No art/broken.mp3').write_bytes(b'generated invalid audio')
    (media / 'No art/unsupported.aac').write_bytes(b'generated unsupported extension')
    (media / 'No art/note.txt').write_text('Generated only. Not a track.\n')
    (media / 'Large library/cover.png').write_bytes(artwork('LOCAL COLLECTION'))
    for number in range(1, 261):
        write_audio(media / 'Large library' / f'Track {number}.wav')
    write_audio(media / 'Root track.wav')
    # A separately registered root can contain the exact same relative name.
    write_audio(media.parent / 'second-music/Album 2/Track 2.wav', seconds=20)
    seed_music_albums(media.parent / 'album-music')
    mirror = media.parent / 'album-mirror/Blue/Disc 1/01 - First.flac'
    mirror.parent.mkdir(parents=True)
    mirror.write_bytes((media.parent / 'album-music/Blue/Disc 1/01 - First.flac').read_bytes())


def seed_music_albums(media: Path):
    import imageio_ffmpeg
    media.mkdir()
    template = media / 'template.flac'
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
                    '-f', 'lavfi', '-i', 'anullsrc=r=8000:cl=mono', '-c:a', 'flac', '-t', '20', str(template)], check=True, timeout=30)
    original = template.read_bytes()
    template.rename(media / 'Root track.flac')
    cases = [
        ('Blue/Disc 1/02 - Later.flac', 'Blue', 'A later song', '歌手 二', 1, 2),
        ('Blue/Disc 1/01 - First.flac', 'Blue', 'Opening song', 'Artist one', 1, 1),
        ('Blue/Disc 2/01 - Finale.flac', 'Blue', 'Finale 曲', 'Artist one', 2, 1),
        ('Copy/Disc 1/copy.flac', 'Blue', 'Separate copy', 'Artist one', 1, 1),
        ('Red/Part A/one.flac', 'Red', 'Red first', 'Artist red', 1, 1),
        ('Red/Part B/two.flac', 'Red', 'Red second', 'Artist red', 2, 1),
        ('Mixed/one.flac', 'One', 'First collection', 'Artist mixed', 1, 1),
        ('Mixed/two.flac', 'Other', 'Second collection', 'Artist mixed', 1, 1),
        ('Long/file.flac', 'A long album name with quiet rain and a distant train 曲 한글', 'A long song title that remains readable in a narrow album table 曲 한글', 'Artist long', 1, 1),
        ('Literal/file.flac', '100%_Music', '100%_Song', 'Literal artist', 1, 1),
    ]
    for relative, album, title, artist, disc, track in cases:
        path = media / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(original)
        flac_comments(path, [f'TITLE={title}', f'ALBUM={album}', 'ALBUMARTIST=Album artist',
                             f'ARTIST={artist}', f'DISCNUMBER={disc}', f'TRACKNUMBER={track}'])
    (media / 'Blue/cover.png').write_bytes(artwork('BLUE', '#725034', '#edc68a'))
    (media / 'Red/cover.png').write_bytes(artwork('RED'))
    for relative in ['Untagged/CD 1/Track 10.wav', 'Untagged/CD 1/Track 2.wav',
                     'Untagged/CD 2/曲.wav', 'Ambiguous/Part A/unlabelled.wav',
                     'Ambiguous/Part B/unlabelled.wav', 'No art/no cover.wav']:
        write_audio(media / relative, seconds=20)
    (media / 'Unknown').mkdir(); (media / 'Unknown/Corrupt.mp3').write_bytes(b'generated corrupt audio')


def seed_files_metadata(media: Path, home: Path):
    """Separate Files-only samples; no module starts or live capture requests."""
    import json
    import sqlite3
    from files_base import index, sources
    from modules.youtube import _publish
    from modules.youtube import metadata, saved_item
    from browser_twitter_fixture import write_capture
    media.mkdir()
    channel = 'UC' + 'a'*22
    info = {'source_id': '', 'youtube': [], 'twitter': []}
    with sqlite3.connect(home / 'user.sqlite') as connection:
        connection.row_factory = sqlite3.Row
        sources.ensure_sources_schema(connection); _publish(connection)
        source = sources.register_source(connection, media, 'Saved metadata')
        info['source_id'] = source.source_id
        for name, current in [('legacy', False), ('current', True)]:
            job_id = name + '-saved-job'
            bundle = f'YouTube/Channel [{channel}]/Saved [{job_id[:16]}]' if current else 'legacy'
            folder = media / bundle; folder.mkdir(parents=True)
            (folder / 'video.webm').write_bytes(b'generated original media')
            (folder / 'thumbnail.png').write_bytes(artwork('SAVED'))
            value = dict(id=job_id, url='https://www.youtube.com/watch?v=abcdefghijk',
                source_id=source.source_id, root='D:\\old\\Windows\\location',
                video='720', audio='192', path=bundle+'/video.webm',
                thumbnail=bundle+'/thumbnail.png', title='Saved song 한글 <script>window.bad=1</script>', status='complete')
            if current:
                capture = metadata.capture({'id':'abcdefghijk','channel_id':channel,'channel':'Captured channel',
                    'duration':125,'format_id':'247+251','vcodec':'vp9','acodec':'opus'}, value['url'], 'video.webm')
                _, _, digest = saved_item.prepare(folder, value, {'path':'video.webm','thumbnail':'thumbnail.png','title':value['title']}, capture)
                value.update(capture, layout_version=1, bundle_path=bundle, manifest_sha256=digest)
            names = list(value)
            connection.execute('INSERT INTO youtube_downloads('+','.join(names)+') VALUES('+','.join('?' for _ in names)+')', list(value.values()))
            info['youtube'].append(value['path'])
        connection.commit()
    for number, options in [(100, {}), (101, {'photos':{'photo-1.jpg':artwork('PHOTO')}}),
                             (102, {'photos':{},'current_format':True})]:
        info['twitter'].append(write_capture(media, number, text='  Preserved 한글\n<img src="https://example.invalid/never">  ', **options))
    (media / 'ordinary.webm').write_bytes(b'unrelated ordinary original')
    (media / 'fixture-info.json').write_text(json.dumps(info))
    with index.open_index(home / 'base/files.sqlite') as connection:
        index.scan_source(connection, source.source_id, media)

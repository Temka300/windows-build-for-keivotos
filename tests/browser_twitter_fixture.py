"""Generated complete/incomplete Twitter archives; no network or user media."""
from datetime import datetime, timedelta, timezone
from io import BytesIO
import hashlib
import json
from pathlib import Path
import subprocess

from PIL import Image

RAW_TEXT = '  한글 世界\nline two \t\n'


def write_capture(root: Path, number=0, *, post_id=None, text=RAW_TEXT,
                  author_id='123', handle='SavedAuthor', video=b'generated-video', avatar=b'generated-avatar'):
    captured = datetime(2026, 10, 11, 12, tzinfo=timezone.utc) + timedelta(seconds=number)
    stamp = captured.isoformat().replace('+00:00', 'Z')
    capture_id = captured.strftime('%Y%m%dT%H%M%S%fZ') + f'-{number:08x}'
    post_id = post_id or str(2000000 + number)
    relative = f'captures/{post_id}/{capture_id}'
    directory = root / relative
    directory.mkdir(parents=True, exist_ok=False)
    url = f'https://x.com/{handle}/status/{post_id}'
    tweet = {'schema_version': 1, 'tweet_id': post_id, 'capture_id': capture_id,
             'source_url': url, 'captured_at': stamp, 'posted_at': '2026-10-10T12:00:00Z',
             'raw_text': text, 'author': {'id': author_id, 'name': 'Saved author 이름', 'handle': handle}}
    records = {'source.json': b'{"fixture":"original raw response"}\n',
               'tweet.json': (json.dumps(tweet, ensure_ascii=False) + '\n').encode('utf-8'),
               'avatar.jpg': avatar, 'video.mp4': video}
    for name, data in records.items():
        (directory / name).write_bytes(data)
    manifest = {'schema_version': 1, 'post_id': post_id, 'capture_id': capture_id,
                'source_url': url, 'captured_at': stamp, 'completed_at': stamp, 'complete': True,
                'files': {name: {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                          for name, data in records.items()}}
    (directory / 'manifest.json').write_text(json.dumps(manifest) + '\n', encoding='utf-8')
    return f'{relative}/manifest.json'


def seed_twitter(media: Path):
    import imageio_ffmpeg
    media.mkdir()
    generated = media / 'generated.mp4'
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
                    '-f', 'lavfi', '-i', 'color=c=blue:s=96x96:r=12', '-t', '3',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
                    str(generated)], check=True, timeout=30)
    picture = BytesIO()
    Image.new('RGB', (48, 48), '#7453b7').save(picture, 'JPEG')
    video, avatar = generated.read_bytes(), picture.getvalue()
    for number in range(68):
        write_capture(media, number, video=video, avatar=avatar)
    incomplete = media / write_capture(media, 68, video=video, avatar=avatar)
    value = json.loads(incomplete.read_text())
    value['complete'] = False
    incomplete.write_text(json.dumps(value), encoding='utf-8')
    (media / 'fixture-info.json').write_text(json.dumps({'video_size': len(video), 'raw_text': RAW_TEXT}), encoding='utf-8')

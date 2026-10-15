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
                  author_id='123', handle='SavedAuthor', video=b'generated-video', avatar=b'generated-avatar', photos=None,
                  avatar_file='avatar.jpg', current_format=False):
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
               avatar_file: avatar, 'video.mp4': video}
    if photos is not None:
        tweet.update(schema_version=2, media=[{'type': 'photo', 'name': name} for name in photos])
        records.pop('video.mp4')
        records.update(photos)
        records['tweet.json']=(json.dumps(tweet, ensure_ascii=False)+'\n').encode('utf-8')
    if current_format:
        tweet.update(schema_version=3, avatar=avatar_file,
                     media=tweet.get('media', [{'type':'video','name':'video.mp4'}]))
        records['tweet.json']=(json.dumps(tweet, ensure_ascii=False)+'\n').encode('utf-8')
    for name, data in records.items():
        (directory / name).write_bytes(data)
    manifest = {'schema_version': 1, 'post_id': post_id, 'capture_id': capture_id,
                'source_url': url, 'captured_at': stamp, 'completed_at': stamp, 'complete': True,
                'files': {name: {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                          for name, data in records.items()}}
    manifest['schema_version']=tweet['schema_version']
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


def source_response(post_id='333'):
    value = {'id_str': post_id, 'text': RAW_TEXT, 'created_at': '2026-10-10T12:00:00Z',
            'user': {'id_str': '123', 'name': 'Saved author 이름', 'screen_name': 'SavedAuthor',
                     'profile_image_url_https': 'https://pbs.twimg.com/generated.jpg'},
            'mediaDetails': [{'type': 'video', 'video_info': {'variants': [
                {'content_type': 'video/mp4', 'bitrate': 128000, 'url': 'https://video.twimg.com/generated.mp4'}]}}]}
    if post_id=='888':
        value['mediaDetails']=[{'type':'photo','media_url_https':f'https://pbs.twimg.com/media/generated-{number}.jpg'} for number in range(1,5)]
    if post_id=='999':
        value.pop('mediaDetails')
        value['text']='Text-only fixture 한글\nline two  '
    return value


def fixture_worker(endpoint):
    """Test-only adapter: actual loopback HTTP and installed yt-dlp, no X traffic.

    Production host validation remains in force on the original source/CDN URLs;
    only the test transport maps their bytes to the generated loopback fixture.
    This function is called by a private/test helper wrapper, never application code.
    """
    from urllib.parse import urlsplit, parse_qs
    from urllib.request import urlopen
    from yt_dlp.networking.common import Request
    from yt_dlp.networking._urllib import UrllibRH
    from modules.twitter import acquisition, worker
    endpoint_parts = urlsplit(endpoint)
    assert endpoint_parts.scheme == 'http' and endpoint_parts.hostname == '127.0.0.1'
    def read(url, host, limit):
        acquisition.remote_url(url, host)
        post_id = parse_qs(urlsplit(url).query).get('id', ['333'])[0]
        if host == 'cdn.syndication.twimg.com': path='/source.json?id='+post_id
        elif urlsplit(url).path.startswith('/media/generated-'): path='/photo-'+urlsplit(url).path.split('generated-')[1]
        else: path='/avatar.jpg'
        with urlopen(endpoint + path, timeout=10) as response:
            data = response.read(limit + 1)
        if not data or len(data) > limit:
            raise ValueError('Fixture response exceeds the capture limit')
        return data
    class Log:
        def debug(self, value): pass
        def warning(self, value): pass
        def error(self, value): pass
    class Transport:
        def __init__(self):
            self.inner = UrllibRH(logger=Log(), proxies={}, timeout=10)
        def send(self, request):
            acquisition.remote_url(request.url, 'video.twimg.com')
            response = self.inner.send(Request(endpoint + '/video.mp4', headers=request.headers,
                                               method=request.method))
            response.url = request.url
            return response
        def close(self):
            self.inner.close()
    acquisition.read_remote = read
    acquisition.media_transport = Transport
    return worker.main([])


def install(media):
    """Install a generated loopback source for the actual browser/server harness."""
    import asyncio
    import sys
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlsplit, parse_qs
    from modules.twitter import jobs
    first = sorted((media / 'captures').glob('*/*/avatar.jpg'))[0]
    avatar, video = first.read_bytes(), (media / 'generated.mp4').read_bytes()
    photos={}
    for number,color in enumerate(('red','blue','green','yellow'),1):
        picture=BytesIO();Image.new('RGB',(80+number*8,96),color).save(picture,'JPEG')
        photos[f'/photo-{number}.jpg']=picture.getvalue()
    state = {'fail': False, 'delay': 0, 'source_requests': 0, 'video_requests': 0, 'avatar_format': 'jpg'}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            assert self.path == '/control'
            value = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
            state.update({key: value[key] for key in ('fail', 'delay', 'avatar_format') if key in value})
            self.respond(json.dumps(state).encode(), 'application/json')
        def do_HEAD(self): self.get(False)
        def do_GET(self): self.get(True)
        def get(self, body):
            url = urlsplit(self.path)
            if url.path == '/stats':
                self.respond(json.dumps(state).encode(), 'application/json', body)
            elif url.path == '/source.json':
                state['source_requests'] += 1
                time.sleep(min(5, max(0, state['delay'])))
                post_id = parse_qs(url.query).get('id', ['333'])[0]
                value = source_response(post_id)
                if state['fail']: value['mediaDetails'] = [{'type':'unsupported'}]
                self.respond(json.dumps(value, ensure_ascii=False).encode('utf-8'), 'application/json', body)
            elif url.path == '/avatar.jpg':
                if state['avatar_format'] == 'png':
                    image=BytesIO();Image.new('RGB',(48,48),'purple').save(image,'PNG')
                    self.respond(image.getvalue(),'image/png',body)
                else:
                    self.respond(avatar, 'image/jpeg', body)
            elif url.path in photos: self.respond(photos[url.path], 'image/jpeg', body)
            elif url.path == '/video.mp4':
                state['video_requests'] += 1
                self.respond(video, 'video/mp4', body)
            else:
                self.send_error(404)
        def respond(self, data, kind, body=True):
            self.send_response(200); self.send_header('Content-Length', str(len(data)))
            self.send_header('Content-Type', kind); self.end_headers()
            if body:
                try: self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f'http://127.0.0.1:{server.server_port}'
    (media / 'fixture-endpoint.json').write_text(json.dumps({'url': endpoint}), encoding='utf-8')
    original = asyncio.create_subprocess_exec
    root = Path(__file__).resolve().parents[1]
    script = 'import sys;sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[2]);from tests.browser_twitter_fixture import fixture_worker;raise SystemExit(fixture_worker(sys.argv[3]))'
    async def launch(*args, **kwargs):
        if '--twitter-worker' in args:
            return await original(sys.executable, '-c', script, str(root), str(root / 'backend'), endpoint, **kwargs)
        return await original(*args, **kwargs)
    asyncio.create_subprocess_exec = launch
    return server

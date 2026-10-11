"""Anonymous syndication adapter and existing yt-dlp HTTP downloader.

Only fixed X CDN hosts are admitted, including every redirect before it is
followed. Cookies, proxies, extractors for arbitrary URLs and browser engines
are not used. Every output is create-only in a caller-owned fresh directory.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from files_base import serving
from modules.twitter import archive
from modules.twitter.helpers import ffmpeg_executable


def remote_url(value, host):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 33 for c in value) or '\\' in value:
        raise ValueError('Unsupported remote media URL')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or parsed.hostname != host or parsed.port not in (None, 443)
            or parsed.username is not None or parsed.password is not None or parsed.fragment):
        raise ValueError('Unsupported remote media URL')
    return value


class Redirects(HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def __init__(self, host):
        self.host = host

    def redirect_request(self, request, fp, code, message, headers, newurl):
        remote_url(newurl, self.host)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def read_remote(url, host, limit):
    remote_url(url, host)
    opener = build_opener(ProxyHandler({}), Redirects(host))
    with opener.open(Request(url, headers={'User-Agent': 'Googlebot', 'Accept-Encoding': 'identity'}), timeout=20) as response:
        remote_url(response.url, host)
        length = response.headers.get('Content-Length')
        if length is not None and int(length) > limit:
            raise ValueError('Remote response exceeds the capture limit')
        data = response.read(limit + 1)
    if not data or len(data) > limit:
        raise ValueError('Empty or oversized remote response')
    return data


def directory(spec):
    root = Path(spec['root'])
    relative = spec['path'].rsplit('/', 1)[0]
    result = serving.resolve_within_source(root, relative, [Path(p) for p in spec['forbidden']])
    # Resolving within the source also fences suite-private and nested owners;
    # preserve the actual directory identity across every acquisition phase.
    if (str(root.resolve()) != spec['root'] or not result.is_dir() or
            list((result.stat().st_dev, result.stat().st_ino)) != spec['directory_identity']):
        raise ValueError('Capture directory changed; files were preserved')
    cursor = root
    for part in relative.split('/'):
        cursor /= part
        if cursor.is_symlink():
            raise ValueError('Capture directory contains a symbolic link')
    return result


def new_file(spec, name, content):
    if name not in archive.ASSET_LIMITS or not 0 < len(content) <= archive.ASSET_LIMITS[name]:
        raise ValueError('Unsupported capture asset')
    with (directory(spec) / name).open('xb') as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def record(spec, source):
    if not isinstance(source, dict) or source.get('id_str') != spec['post_id']:
        raise ValueError('Source response does not match the requested post')
    user = source.get('user')
    if not isinstance(user, dict):
        raise ValueError('Post has no supported author')
    author_id = user.get('id_str')
    if author_id is not None and (not isinstance(author_id, str) or re.fullmatch(archive.POST_ID, author_id) is None):
        raise ValueError('Post author ID is unsupported')
    handle = archive._text(user.get('screen_name'), 64, 'author handle')
    if re.fullmatch(archive.HANDLE, handle) is None:
        raise ValueError('Post author handle is unsupported')
    text = archive._text(source.get('text'), 262144, 'post text')
    author = {'id': author_id, 'name': archive._text(user.get('name'), 256, 'author name'), 'handle': handle}
    media = source.get('mediaDetails')
    if not isinstance(media, list) or len(media) != 1 or not isinstance(media[0], dict) or media[0].get('type') != 'video':
        raise ValueError('Only a post with one direct MP4 video is supported')
    video_info = media[0].get('video_info') or {}
    variants = video_info.get('variants') if isinstance(video_info, dict) else None
    if not isinstance(variants, list):
        raise ValueError('Post has no direct MP4 video')
    candidates = [v for v in variants if isinstance(v, dict) and v.get('content_type') == 'video/mp4'
                  and type(v.get('bitrate', 0)) is int and v.get('bitrate', 0) >= 0]
    if not candidates:
        raise ValueError('HLS-only video is not supported')
    best = max(candidates, key=lambda v: v.get('bitrate', 0))
    video_url = remote_url(best.get('url'), 'video.twimg.com')
    avatar_url = remote_url(user.get('profile_image_url_https'), 'pbs.twimg.com')
    posted = source.get('created_at')
    if posted is not None:
        archive._utc(posted)
    tweet = {'schema_version': 1, 'tweet_id': spec['post_id'], 'capture_id': spec['capture_id'],
             'source_url': spec['url'], 'captured_at': spec['captured_at'], 'posted_at': posted,
             'raw_text': text, 'author': author,
             'retrieval': {'method': 'anonymous syndication and direct CDN', 'account_used': False}}
    return tweet, avatar_url, video_url


def media_transport():
    """Use the installed HTTP handler with a pre-follow CDN redirect boundary."""
    from yt_dlp.networking._urllib import UrllibRH

    class GuardedTransport(UrllibRH):
        def _create_instance(self, proxies, cookiejar, legacy_ssl_support=None):
            original = super()._create_instance({}, cookiejar, legacy_ssl_support)
            from urllib.request import OpenerDirector
            opener = OpenerDirector()
            for handler in original.handlers:
                if not isinstance(handler, HTTPRedirectHandler):
                    opener.add_handler(handler)
            opener.add_handler(Redirects('video.twimg.com'))
            opener.addheaders = []
            return opener

    class Log:
        def debug(self, message): pass
        def warning(self, message): pass
        def error(self, message): pass

    return GuardedTransport(logger=Log(), proxies={}, timeout=20)


def download_video(spec, url, notify):
    from yt_dlp import YoutubeDL
    from yt_dlp.networking.common import Request as MediaRequest

    class Downloader(YoutubeDL):
        def urlopen(self, request):
            if isinstance(request, str):
                request = MediaRequest(request)
            remote_url(request.url, 'video.twimg.com')
            return transport.send(request)

    last = 0.0
    def progress(value):
        nonlocal last
        directory(spec)
        downloaded = value.get('downloaded_bytes') or 0
        total = value.get('total_bytes') or value.get('total_bytes_estimate') or 0
        if max(downloaded, total) > archive.ASSET_LIMITS['video.mp4']:
            raise ValueError('Video exceeds the capture limit')
        now = time.monotonic()
        if now - last >= .2 or value.get('status') == 'finished':
            notify('video', min(90, 10 + downloaded / total * 80) if total else 10)
            last = now
    output = directory(spec) / 'video.mp4'
    if output.exists() or output.with_suffix('.mp4.part').exists():
        raise ValueError('Capture output already exists')
    transport = media_transport()
    try:
        with Downloader({'outtmpl': str(output), 'cachedir': False, 'quiet': True, 'noprogress': True,
                         'noplaylist': True, 'overwrites': False, 'continuedl': False,
                         'retries': 2, 'socket_timeout': 20, 'concurrent_fragment_downloads': 1,
                         'max_filesize': archive.ASSET_LIMITS['video.mp4'], 'js_runtimes': {},
                         'remote_components': set(), 'progress_hooks': [progress]}) as downloader:
            result = downloader.download([remote_url(url, 'video.twimg.com')])
        if result or not output.is_file():
            raise ValueError('Video download did not produce a completed MP4')
    finally:
        transport.close()


def verify(spec):
    from PIL import Image
    ffmpeg = ffmpeg_executable()
    if not ffmpeg:
        raise ValueError('FFmpeg is unavailable')
    folder = directory(spec)
    with Image.open(folder / 'avatar.jpg') as image:
        if image.format != 'JPEG' or image.width * image.height > 16_000_000:
            raise ValueError('Only a bounded JPEG avatar is supported')
        image.verify()
    decoded = subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-xerror', '-f', 'mp4', '-i',
                              str(folder / 'video.mp4'), '-map', '0:v:0', '-f', 'null', '-'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
    if decoded.returncode or decoded.stderr.strip():
        raise ValueError('Saved MP4 verification failed')
    files = {}
    for name, maximum in archive.ASSET_LIMITS.items():
        target = serving.resolve_served_file(spec['root'], f"{spec['path'].rsplit('/', 1)[0]}/{name}",
                                              [Path(p) for p in spec['forbidden']])
        if target.is_symlink() or not 0 < target.stat().st_size <= maximum:
            raise ValueError('Capture asset is missing, linked or oversized')
        before = target.stat()
        with target.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
            after = os.fstat(handle.fileno())
        if archive._identity(before) != archive._identity(after) or archive._identity(before) != archive._identity(target.stat()):
            raise ValueError('Capture asset changed during verification')
        files[name] = {'size': before.st_size, 'sha256': digest,
                       'identity': list(archive._identity(before))}
    directory(spec)
    return files


def capture(spec, notify):
    from yt_dlp.jsinterp import js_number_to_string
    url, post_id = archive.canonical_url(spec['url'])
    if url != spec['url'] or post_id != spec['post_id']:
        raise ValueError('Invalid capture specification')
    archive._path_parts(spec['path'])
    if spec['capture_id'].split('-')[0] != archive._utc(spec['captured_at']).strftime('%Y%m%dT%H%M%S%fZ'):
        raise ValueError('Invalid capture identity')
    directory(spec)
    notify('metadata', 1)
    token = js_number_to_string((int(post_id) / 1e15) * math.pi, 36).translate(str.maketrans('', '', '0.'))
    endpoint = 'https://cdn.syndication.twimg.com/tweet-result?' + urlencode({'id': post_id, 'token': token})
    raw = read_remote(endpoint, 'cdn.syndication.twimg.com', archive.ASSET_LIMITS['source.json'])
    new_file(spec, 'source.json', raw)
    source = json.loads(raw, object_pairs_hook=archive._unique_object)
    tweet, avatar_url, video_url = record(spec, source)
    notify('avatar', 5)
    new_file(spec, 'avatar.jpg', read_remote(avatar_url, 'pbs.twimg.com', archive.ASSET_LIMITS['avatar.jpg']))
    new_file(spec, 'tweet.json', (json.dumps(tweet, ensure_ascii=False) + '\n').encode('utf-8'))
    notify('video', 10)
    download_video(spec, video_url, notify)
    notify('verifying', 92)
    return verify(spec)

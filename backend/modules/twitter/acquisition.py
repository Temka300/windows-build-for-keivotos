"""Anonymous syndication adapter and existing yt-dlp HTTP downloader.

Only fixed X CDN hosts are admitted, including every redirect before it is
followed. Cookies, proxies, extractors for arbitrary URLs and browser engines
are not used. Every output is create-only in a caller-owned fresh directory.
"""
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlencode, urlsplit, urlunsplit
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
    maximum = {**archive.ASSET_LIMITS, **archive.PHOTO_LIMITS, **archive.AVATAR_LIMITS}.get(name, 0)
    if not 0 < len(content) <= maximum:
        raise ValueError('Unsupported capture asset')
    with (directory(spec) / name).open('xb') as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


class AnonymousCaptureUnavailable(ValueError):
    code = 'twitter_anonymous_unavailable'

    def __init__(self):
        super().__init__(f'[{self.code}] This post is unavailable through anonymous capture')


def record(spec, source):
    if not isinstance(source, dict):
        raise ValueError('Source response is not a supported post object')
    source_id = source.get('id_str')
    if source_id is not None and source_id != spec['post_id']:
        raise ValueError('Source response does not match the requested post')
    if (not source or source.get('__typename') in ('TweetTombstone', 'TweetUnavailable') or
            isinstance(source.get('tombstone'), dict)):
        raise AnonymousCaptureUnavailable()
    if source_id is None:
        raise ValueError('Source response has no supported post ID')
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
    media = source.get('mediaDetails', [])
    if (not isinstance(media, list) or not 0 <= len(media) <= 4 or
            not all(isinstance(item, dict) for item in media)):
        raise ValueError('Only one direct MP4 video or one to four photos are supported')
    text_only = not media
    if text_only and not text.strip():
        raise ValueError('Text-only post has no text')
    photo_urls = []
    if media and all(item.get('type') == 'photo' for item in media):
        for number, item in enumerate(media, 1):
            parsed = urlsplit(remote_url(item.get('media_url_https'), 'pbs.twimg.com'))
            if not re.fullmatch(r'/media/[A-Za-z0-9_-]+\.(?:jpg|jpeg|png|webp)', parsed.path) or parsed.query:
                raise ValueError('Unsupported photo URL')
            extension = parsed.path.rsplit('.', 1)[1]
            extension = 'jpg' if extension == 'jpeg' else extension
            # Request the CDN original without transcoding or rewriting its bytes.
            url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, 'name=orig', ''))
            photo_urls.append((f'photo-{number}.{extension}', url))
    elif media and (len(media) != 1 or media[0].get('type') != 'video'):
        raise ValueError('Only one direct MP4 video or one to four photos are supported')
    video_info = (media[0].get('video_info') or {}) if media else {}
    variants = video_info.get('variants') if isinstance(video_info, dict) else None
    if not text_only and not photo_urls and not isinstance(variants, list):
        raise ValueError('Post has no direct MP4 video')
    candidates = [v for v in (variants or []) if isinstance(v, dict) and v.get('content_type') == 'video/mp4'
                  and type(v.get('bitrate', 0)) is int and v.get('bitrate', 0) >= 0]
    if not text_only and not photo_urls and not candidates:
        raise ValueError('HLS-only video is not supported')
    video_url = remote_url(max(candidates, key=lambda v: v.get('bitrate', 0)).get('url'), 'video.twimg.com') if not text_only and not photo_urls else None
    avatar_url = remote_url(user.get('profile_image_url_https'), 'pbs.twimg.com')
    posted = source.get('created_at')
    if posted is not None:
        archive._utc(posted)
    tweet = {'schema_version': 3, 'tweet_id': spec['post_id'], 'capture_id': spec['capture_id'],
             'source_url': spec['url'], 'captured_at': spec['captured_at'], 'posted_at': posted,
             'raw_text': text, 'author': author,
             'retrieval': {'method': 'anonymous syndication and direct CDN', 'account_used': False}}
    if photo_urls:
        tweet['media'] = [{'type': 'photo', 'name': name} for name, _ in photo_urls]
    elif not text_only:
        tweet['media'] = [{'type': 'video', 'name': 'video.mp4'}]
    else:
        tweet['media'] = []
    return tweet, avatar_url, [] if text_only else photo_urls or video_url


def avatar_name(content):
    """Detect and decode bounded original artwork without changing its bytes."""
    from PIL import Image
    formats = {'JPEG': 'jpg', 'PNG': 'png', 'WEBP': 'webp'}
    if not 0 < len(content) <= archive.ASSET_LIMITS['avatar.jpg']:
        raise ValueError('Avatar exceeds the capture limit')
    with Image.open(BytesIO(content)) as image:
        extension = formats.get(image.format)
        if extension is None or image.width * image.height > 16_000_000 or getattr(image, 'n_frames', 1) != 1:
            raise ValueError('Only bounded static JPEG, PNG or WebP avatars are supported')
        image.verify()
    with Image.open(BytesIO(content)) as image:
        image.load()
    return f'avatar.{extension}'


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
    folder = directory(spec)
    target = serving.resolve_served_file(spec['root'], f"{spec['path'].rsplit('/', 1)[0]}/tweet.json",
                                         [Path(p) for p in spec['forbidden']])
    with target.open('rb') as handle:
        raw = handle.read(archive.TWEET_LIMIT + 1)
    if len(raw) > archive.TWEET_LIMIT:
        raise ValueError('Tweet metadata is too large')
    tweet = json.loads(raw, object_pairs_hook=archive._unique_object)
    limits = archive.tweet_asset_limits(tweet)
    resolved = {}
    for name, maximum in limits.items():
        target = serving.resolve_served_file(spec['root'], f"{spec['path'].rsplit('/', 1)[0]}/{name}",
                                             [Path(p) for p in spec['forbidden']])
        if target.is_symlink() or not 0 < target.stat().st_size <= maximum:
            raise ValueError('Capture asset is missing, linked or oversized')
        resolved[name] = target
    avatar = next(name for name in limits if name in archive.AVATAR_LIMITS)
    with Image.open(resolved[avatar]) as image:
        expected = {'jpg': 'JPEG', 'png': 'PNG', 'webp': 'WEBP'}[avatar.rsplit('.', 1)[1]]
        if image.format != expected or image.width * image.height > 16_000_000 or getattr(image, 'n_frames', 1) != 1:
            raise ValueError('Only bounded static JPEG, PNG or WebP avatars are supported')
        image.verify()
    with Image.open(resolved[avatar]) as image:
        image.load()
    for name in limits:
        if name not in archive.PHOTO_LIMITS:
            continue
        with Image.open(resolved[name]) as image:
            expected = {'jpg': 'JPEG', 'png': 'PNG', 'webp': 'WEBP'}[name.rsplit('.', 1)[1]]
            if image.format != expected or image.width * image.height > 24_000_000 or getattr(image, 'n_frames', 1) != 1:
                raise ValueError('Only bounded static JPEG, PNG or WebP photos are supported')
            image.verify()
        with Image.open(resolved[name]) as image:
            image.load()
    if 'video.mp4' in limits:
        ffmpeg = ffmpeg_executable()
        if not ffmpeg:
            raise ValueError('FFmpeg is unavailable')
        decoded = subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-xerror', '-f', 'mp4', '-i',
                              str(resolved['video.mp4']), '-map', '0:v:0', '-f', 'null', '-'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
        if decoded.returncode or decoded.stderr.strip():
            raise ValueError('Saved MP4 verification failed')
    files = {}
    for name, maximum in limits.items():
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
    tweet, avatar_url, media = record(spec, source)
    notify('avatar', 5)
    avatar = read_remote(avatar_url, 'pbs.twimg.com', archive.ASSET_LIMITS['avatar.jpg'])
    tweet['avatar'] = avatar_name(avatar)
    new_file(spec, tweet['avatar'], avatar)
    new_file(spec, 'tweet.json', (json.dumps(tweet, ensure_ascii=False) + '\n').encode('utf-8'))
    if isinstance(media, list):
        for number, (name, url) in enumerate(media):
            notify('photos', 10 + number / len(media) * 80)
            new_file(spec, name, read_remote(url, 'pbs.twimg.com', archive.PHOTO_LIMITS[name]))
    else:
        notify('video', 10)
        download_video(spec, media, notify)
    notify('verifying', 92)
    return verify(spec)

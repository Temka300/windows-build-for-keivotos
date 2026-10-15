"""Bounded, read-only completed captures from the shared Files index.

The format admits fixed local assets. Files owns resolved containment, private
tree and nested-source guards. Browsing hashes small metadata only; acquisition
verifies full original assets before publishing the completion manifest.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from files_base import index, serving, sources

MANIFEST_LIMIT = 64 * 1024
TWEET_LIMIT = 1024 * 1024
PAGE_BYTE_LIMIT = 4 * 1024 * 1024
ASSET_LIMITS = {'source.json': 4 * 1024 * 1024, 'tweet.json': TWEET_LIMIT,
                'avatar.jpg': 10 * 1024 * 1024, 'video.mp4': 4 * 1024 ** 3}
PHOTO_LIMITS = {f'photo-{number}.{extension}': 32 * 1024 * 1024
                for number in range(1, 5) for extension in ('jpg', 'png', 'webp')}
AVATAR_LIMITS = {f'avatar.{extension}': ASSET_LIMITS['avatar.jpg'] for extension in ('jpg', 'png', 'webp')}
POST_ID = r'[1-9][0-9]{0,21}'
HANDLE = r'[A-Za-z0-9_]{1,64}'
CAPTURE_ID = r'[0-9]{8}T[0-9]{12}Z-[a-f0-9]{8}'
MANIFEST_PATH = re.compile(rf'captures/({POST_ID})/({CAPTURE_ID})/manifest\.json')
STATUS_PATH = re.compile(rf'/(?:{HANDLE}|i/web)/status/({POST_ID})/?')
HASH = re.compile(r'[a-f0-9]{64}')


class InvalidCapture(ValueError):
    """The preserved record is incomplete, inconsistent or outside its format."""


class PageFull(Exception):
    """The next record belongs to the next bounded page."""


def _current_asset_limits(files):
    avatars = set(files) & set(AVATAR_LIMITS)
    if len(avatars) != 1:
        raise InvalidCapture('Capture must declare exactly one supported avatar')
    avatar = next(iter(avatars))
    base = {'source.json': ASSET_LIMITS['source.json'], 'tweet.json': TWEET_LIMIT,
            avatar: AVATAR_LIMITS[avatar]}
    media = set(files) - set(base)
    if not set(base) <= set(files):
        raise InvalidCapture('Capture metadata assets are missing')
    if not media:
        return base
    if media == {'video.mp4'}:
        return {**base, 'video.mp4': ASSET_LIMITS['video.mp4']}
    if not 1 <= len(media) <= 4 or not media <= set(PHOTO_LIMITS):
        raise InvalidCapture('Capture media assets are invalid')
    ordered = sorted(media)
    if [int(name.split('-')[1].split('.')[0]) for name in ordered] != list(range(1, len(media) + 1)):
        raise InvalidCapture('Capture photos are not in their original order')
    return {**base, **{name: PHOTO_LIMITS[name] for name in ordered}}


def asset_limits(files, version=None):
    """Admit legacy video or one to four consecutively numbered photo assets."""
    if not isinstance(files, dict):
        raise InvalidCapture('Capture assets are invalid')
    if version == 3 or version is None and (set(files) & (set(AVATAR_LIMITS) - {'avatar.jpg'}) or
                                           set(files) == {'source.json', 'tweet.json', 'avatar.jpg'}):
        return _current_asset_limits(files)
    if set(files) == set(ASSET_LIMITS) and version in (None, 1):
        return ASSET_LIMITS.copy()
    base = {name: maximum for name, maximum in ASSET_LIMITS.items() if name != 'video.mp4'}
    photos = set(files) - set(base)
    if version not in (None, 2) or not set(base) <= set(files) or not 1 <= len(photos) <= 4 or not photos <= set(PHOTO_LIMITS):
        raise InvalidCapture('Capture assets are invalid')
    ordered = sorted(photos)
    if [int(name.split('-')[1].split('.')[0]) for name in ordered] != list(range(1, len(photos) + 1)):
        raise InvalidCapture('Capture photos are not in their original order')
    return {**base, **{name: PHOTO_LIMITS[name] for name in ordered}}


def tweet_asset_limits(tweet):
    if not isinstance(tweet, dict) or type(tweet.get('schema_version')) is not int:
        raise InvalidCapture('Tweet metadata has invalid format')
    if tweet['schema_version'] == 1:
        return ASSET_LIMITS.copy()
    if tweet['schema_version'] == 3:
        avatar = tweet.get('avatar')
        media = tweet.get('media')
        if not isinstance(avatar, str) or avatar not in AVATAR_LIMITS or not isinstance(media, list) or not 0 <= len(media) <= 4:
            raise InvalidCapture('Tweet metadata has invalid avatar or media')
        if not media and (not isinstance(tweet.get('raw_text'), str) or not tweet['raw_text'].strip()):
            raise InvalidCapture('Text-only capture has no post text')
        names = []
        for item in media:
            if not isinstance(item, dict) or not isinstance(item.get('name'), str):
                raise InvalidCapture('Tweet metadata has invalid media')
            name = item['name']
            if not (item.get('type') == 'video' and name == 'video.mp4' or
                    item.get('type') == 'photo' and name in PHOTO_LIMITS):
                raise InvalidCapture('Tweet metadata has unsupported media')
            names.append(name)
        files = dict.fromkeys(['source.json', 'tweet.json', avatar, *names])
        limits = asset_limits(files, 3)
        if names != [name for name in limits if name == 'video.mp4' or name in PHOTO_LIMITS]:
            raise InvalidCapture('Tweet metadata has inconsistent media order')
        return limits
    media = tweet.get('media')
    if tweet['schema_version'] != 2 or not isinstance(media, list) or not 1 <= len(media) <= 4:
        raise InvalidCapture('Tweet metadata has invalid photos')
    names = []
    for item in media:
        if (not isinstance(item, dict) or item.get('type') != 'photo' or
                not isinstance(item.get('name'), str) or item['name'] not in PHOTO_LIMITS):
            raise InvalidCapture('Tweet metadata has invalid photos')
        names.append(item['name'])
    files = {name: None for name in ASSET_LIMITS if name != 'video.mp4'}
    files.update({name: None for name in names})
    limits = asset_limits(files, 2)
    if names != [name for name in limits if name in PHOTO_LIMITS]:
        raise InvalidCapture('Tweet metadata has inconsistent photo order')
    return limits


class ReadBudget:
    def __init__(self, remaining=PAGE_BYTE_LIMIT):
        self.remaining = remaining

    def consume(self, size):
        if size > self.remaining:
            raise PageFull()
        self.remaining -= size


def canonical_url(value: str) -> tuple[str, str]:
    """Closed HTTPS status-link admission; never performs network access."""
    if not isinstance(value, str) or len(value) > 2048 or re.search(r'[\x00-\x20\\]', value):
        raise InvalidCapture('Unsupported post URL')
    try:
        url = urlsplit(value)
        match = STATUS_PATH.fullmatch(url.path)
        if (url.scheme != 'https' or url.hostname not in
                {'x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'} or
                url.username is not None or url.password is not None or
                url.port not in (None, 443) or match is None):
            raise InvalidCapture('Unsupported post URL')
    except ValueError as error:
        raise InvalidCapture('Unsupported post URL') from error
    return f'https://x.com{url.path.rstrip("/")}', match[1]


def forbidden_roots(source, registered, suite_home):
    return [Path(suite_home), *(Path(child.path)
            for child in sources.descendant_sources(source, registered))]


def available(source, forbidden):
    try:
        return serving.resolve_within_source(source.path, '', forbidden).is_dir()
    except (serving.ServeDenied, OSError, ValueError):
        return False


def _identity(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise InvalidCapture('Capture metadata has duplicate keys')
        value[key] = item
    return value


def _read_json(source, path, forbidden, maximum, budget):
    target = serving.resolve_served_file(source.path, path, forbidden)
    before = target.stat()
    if before.st_size > maximum:
        raise InvalidCapture('Capture metadata is too large')
    budget.consume(before.st_size)
    with target.open('rb') as handle:
        if _identity(os.fstat(handle.fileno())) != _identity(before):
            raise InvalidCapture('Capture changed while reading')
        raw = handle.read(maximum + 1)
        after = os.fstat(handle.fileno())
    resolved = serving.resolve_served_file(source.path, path, forbidden)
    if (len(raw) > maximum or len(raw) != before.st_size or resolved != target or
            _identity(after) != _identity(before) or _identity(resolved.stat()) != _identity(before)):
        raise InvalidCapture('Capture changed while reading')
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError) as error:
        raise InvalidCapture('Capture metadata is invalid') from error
    if not isinstance(value, dict):
        raise InvalidCapture('Capture metadata must be an object')
    return value, raw


def _text(value, maximum, name):
    if not isinstance(value, str) or len(value) > maximum:
        raise InvalidCapture(f'Invalid {name}')
    # Reject unpaired UTF-16 surrogates which cannot be emitted as UTF-8 JSON.
    try:
        value.encode('utf-8')
    except UnicodeError as error:
        raise InvalidCapture(f'Invalid {name}') from error
    return value


def _utc(value):
    value = _text(value, 40, 'timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except ValueError as error:
        raise InvalidCapture('Invalid capture timestamp') from error


def _path_parts(path):
    match = MANIFEST_PATH.fullmatch(path)
    if match is None:
        raise InvalidCapture('Invalid capture path')
    try:
        datetime.strptime(match[2].split('-')[0], '%Y%m%dT%H%M%S%fZ')
    except ValueError as error:
        raise InvalidCapture('Invalid capture ID') from error
    return match[1], match[2]


def capture(source, registered, suite_home, path, budget=None):
    """Validate one completed snapshot without loading source/media bodies."""
    post_id, capture_id = _path_parts(path)
    forbidden = forbidden_roots(source, registered, suite_home)
    budget = budget or ReadBudget()
    manifest, _ = _read_json(source, path, forbidden, MANIFEST_LIMIT, budget)
    if (type(manifest.get('schema_version')) is not int or manifest['schema_version'] not in (1, 2, 3) or
            manifest.get('complete') is not True or manifest.get('post_id') != post_id or
            manifest.get('capture_id') != capture_id):
        raise InvalidCapture('Capture is incomplete or has inconsistent identity')
    captured = _utc(manifest.get('captured_at'))
    if captured.strftime('%Y%m%dT%H%M%S%fZ') != capture_id.split('-')[0]:
        raise InvalidCapture('Capture time does not match its identity')
    completed = _utc(manifest.get('completed_at'))
    if completed < captured:
        raise InvalidCapture('Capture completed before it started')
    source_url, url_id = canonical_url(manifest.get('source_url'))
    if url_id != post_id:
        raise InvalidCapture('Source URL does not match the post')
    assets = manifest.get('files')
    limits = asset_limits(assets, manifest['schema_version'])
    for name, maximum in limits.items():
        asset = assets[name]
        if (not isinstance(asset, dict) or type(asset.get('size')) is not int or
                not 0 < asset['size'] <= maximum or not isinstance(asset.get('sha256'), str) or
                HASH.fullmatch(asset['sha256']) is None):
            raise InvalidCapture('Capture asset facts are invalid')
    parent = path.rsplit('/', 1)[0]
    tweet, raw = _read_json(source, f'{parent}/tweet.json', forbidden, TWEET_LIMIT, budget)
    if len(raw) != assets['tweet.json']['size'] or hashlib.sha256(raw).hexdigest() != assets['tweet.json']['sha256']:
        raise InvalidCapture('Tweet metadata does not match its manifest')
    if (type(tweet.get('schema_version')) is not int or tweet['schema_version'] != manifest['schema_version'] or
            tweet.get('tweet_id') != post_id or tweet.get('capture_id') != capture_id or
            tweet.get('captured_at') != manifest['captured_at'] or
            canonical_url(tweet.get('source_url')) != (source_url, post_id)):
        raise InvalidCapture('Tweet metadata has inconsistent identity')
    if tweet_asset_limits(tweet) != limits:
        raise InvalidCapture('Tweet media does not match its manifest')
    author = tweet.get('author')
    if not isinstance(author, dict):
        raise InvalidCapture('Invalid captured author')
    author_name = _text(author.get('name'), 256, 'author name')
    handle = _text(author.get('handle'), 64, 'author handle')
    if re.fullmatch(HANDLE, handle) is None:
        raise InvalidCapture('Invalid captured handle')
    author_id = author.get('id')
    if author_id is not None and (not isinstance(author_id, str) or re.fullmatch(POST_ID, author_id) is None):
        raise InvalidCapture('Invalid captured author ID')
    text = _text(tweet.get('raw_text'), 262144, 'post text')
    posted_at = tweet.get('posted_at')
    if posted_at is not None:
        _utc(posted_at)
    unavailable = []
    for name, asset in assets.items():
        try:
            target = serving.resolve_served_file(source.path, f'{parent}/{name}', forbidden)
            if target.stat().st_size != asset['size']:
                unavailable.append(name)
        except (serving.ServeDenied, OSError, ValueError):
            unavailable.append(name)
    return {'source_id': source.source_id, 'source_name': source.display_name, 'path': path,
            'post_id': post_id, 'capture_id': capture_id, 'source_url': source_url,
            'captured_at': manifest['captured_at'], 'completed_at': manifest['completed_at'],
            'posted_at': posted_at, 'raw_text': text,
            'author': {'id': author_id, 'name': author_name, 'handle': handle},
            'author_key': f'id:{author_id}' if author_id else f'handle:{handle.casefold()}',
            'avatar_path': f'{parent}/{next(name for name in limits if name in AVATAR_LIMITS)}',
            'video_path': f'{parent}/video.mp4' if 'video.mp4' in assets else None,
            'photos': [{'index': number, 'path': f'{parent}/{name}', 'name': name}
                       for number, name in enumerate((name for name in limits if name in PHOTO_LIMITS), 1)],
            'files': {name: {'size': asset['size'], 'sha256': asset['sha256']} for name, asset in assets.items()},
            'available': not unavailable, 'unavailable_assets': unavailable}


def listing(db, registered, suite_home, offset, limit, query='', author=''):
    """Page indexed candidates; search is literal over bounded saved metadata.

    total counts admitted indexed paths, not matches. next_offset advances over
    every examined candidate, including invalid/nonmatching records, so callers
    can continue filtered searches without retaining all archive metadata.
    """
    visible = {source.source_id: source for source in registered
               if source.role == 'twitter' and source.visible}
    if not visible:
        return {'items': [], 'total': 0, 'next_offset': None, 'invalid': 0}
    def order(source_id, path):
        if source_id not in visible:
            return None
        try:
            _, capture_id = _path_parts(path)
            return capture_id
        except InvalidCapture:
            return None
    with index.open_index(db) as connection:
        connection.create_function('twitter_capture', 2, order, deterministic=True)
        where = "is_dir=0 AND name='manifest.json' AND available=1 AND twitter_capture(source_id,relative_path) IS NOT NULL"
        total = connection.execute(f'SELECT COUNT(*) FROM files_index WHERE {where}').fetchone()[0]
        rows = connection.execute(
            f'SELECT source_id,relative_path FROM files_index WHERE {where} '
            'ORDER BY twitter_capture(source_id,relative_path) DESC,source_id,relative_path LIMIT ? OFFSET ?',
            (limit, offset)).fetchall()
    items, invalid, examined = [], 0, 0
    budget = ReadBudget()
    needle = query.casefold()
    for row in rows:
        try:
            item = capture(visible[row['source_id']], registered, suite_home, row['relative_path'], budget)
        except PageFull:
            break
        except (InvalidCapture, serving.ServeDenied, OSError, ValueError):
            invalid += 1
        else:
            if ((not author or item['author_key'] == author) and (not needle or any(
                    needle in value.casefold() for value in
                    (item['raw_text'], item['author']['name'], item['author']['handle'])))):
                items.append(item)
        examined += 1
    cursor = offset + examined
    return {'items': items, 'total': total, 'next_offset': cursor if cursor < total else None, 'invalid': invalid}

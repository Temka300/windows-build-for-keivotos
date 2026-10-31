"""Bounded, immutable description/engagement snapshots supplied by yt-dlp."""
import hashlib
import os
import re
from datetime import datetime
from pathlib import Path

from storage_layout import portable_path
from services.item_manifest import file_identity
from files_base.serving import ServeDenied

MAX_BYTES = 1024 * 1024
MAX_COUNT = 2**53 - 1
FIELDS = {'version', 'video_id', 'captured_at', 'extractor', 'extractor_version',
          'like_count', 'dislike_count', 'description', 'description_status'}


def _count(value):
    return value if type(value) is int and 0 <= value <= MAX_COUNT else None


def _read(root, relative, forbidden=()):
    path = portable_path(root, relative, forbidden)
    with path.open('rb') as stream:
        before = file_identity(os.fstat(stream.fileno()))
        raw = stream.read(MAX_BYTES + 1)
        if file_identity(os.fstat(stream.fileno())) != before:
            raise ValueError('Saved description changed while reading.')
    if len(raw) > MAX_BYTES or file_identity(path.stat()) != before:
        raise ValueError('Saved description changed or exceeds its limit.')
    portable_path(root, relative, forbidden)
    raw.decode('utf-8')
    return raw


def validate(value, facts):
    if (not isinstance(value, dict) or set(value) != FIELDS or type(value['version']) is not int
            or value['version'] != 1 or value['video_id'] != facts['video_id']
            or value['captured_at'] != facts['captured_at']):
        raise ValueError('Saved details identity changed.')
    try: datetime.strptime(value['captured_at'], '%Y-%m-%dT%H:%M:%SZ')
    except (TypeError, ValueError): raise ValueError('Invalid details capture date.') from None
    for key in ('extractor', 'extractor_version'):
        if not isinstance(value[key], str) or not re.fullmatch(r'[A-Za-z0-9_.+:-]{1,80}', value[key]):
            raise ValueError('Invalid details provenance.')
    for key in ('like_count', 'dislike_count'):
        if value[key] is not None and _count(value[key]) is None:
            raise ValueError('Invalid captured count.')
    asset = value['description']
    if asset is None:
        if value['description_status'] not in ('absent', 'unavailable'):
            raise ValueError('Invalid description availability.')
    elif (not isinstance(asset, dict) or set(asset) != {'path', 'size', 'sha256'}
            or value['description_status'] != 'captured'
            or type(asset['size']) is not int or not 0 <= asset['size'] <= MAX_BYTES
            or not isinstance(asset['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', asset['sha256'])
            or asset['path'] != '.keivotos/d-' + asset['sha256'][:16] + '.txt'):
        raise ValueError('Invalid saved description relationship.')
    return value


def capture(info, directory, facts, *, engine_version):
    """Keep the maintained downloader's complete text, never a truncated excerpt."""
    if info.get('id') != facts['video_id']:
        raise ValueError('Details belong to a different video.')
    value = dict(version=1, video_id=facts['video_id'], captured_at=facts['captured_at'],
                 extractor=info.get('extractor_key') or 'Youtube', extractor_version=engine_version,
                 like_count=_count(info.get('like_count')), dislike_count=_count(info.get('dislike_count')),
                 description=None, description_status='absent')
    supplied = info.get('description')
    if supplied is not None:
        value['description_status'] = 'unavailable'
        try:
            if not isinstance(supplied, str) or len(supplied.encode('utf-8')) > MAX_BYTES:
                return validate(value, facts)
            if supplied == '':
                raw = b''
            else:
                candidates = [name for name in ('video.description', 'audio.description')
                              if portable_path(directory, name, (), must_exist=False).exists()]
                if len(candidates) != 1:
                    return validate(value, facts)
                raw = _read(directory, candidates[0])
                if raw.decode('utf-8').replace('\r\n', '\n') != supplied.replace('\r\n', '\n'):
                    return validate(value, facts)
            digest = hashlib.sha256(raw).hexdigest()
            relative = '.keivotos/d-' + digest[:16] + '.txt'
            private = portable_path(directory, '.keivotos', (), must_exist=False)
            private.mkdir(exist_ok=True)
            target = portable_path(directory, relative, (), must_exist=False)
            try:
                with target.open('xb') as stream:
                    stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            except FileExistsError:
                if _read(directory, relative) != raw:
                    raise ValueError('Description snapshot already exists with different bytes.')
            if _read(directory, relative) != raw:
                raise ValueError('Description snapshot changed.')
            value.update(description=dict(path=relative, size=len(raw), sha256=digest), description_status='captured')
        except (OSError, UnicodeError):
            # Optional text cannot remove otherwise valid media. Preserve all source bytes.
            pass
    return validate(value, facts)


def asset_bytes(root, value, forbidden=()):
    """Validate the recorded optional asset; no alternate files or network lookup."""
    asset = value['description']
    if asset is None:
        return None
    raw = _read(root, asset['path'], forbidden)
    if len(raw) != asset['size'] or hashlib.sha256(raw).hexdigest() != asset['sha256']:
        raise ValueError('Saved description does not match its capture.')
    return raw


def asset_path(value, prefix=''):
    asset = value.get('description') if value else None
    return '/'.join(p for p in (prefix, asset['path']) if p) if asset else ''


def manifest_snapshot(value):
    """Return owner facts and the bundle-relative description context."""
    from modules.youtube import portable
    extension = value['extensions'].get('youtube', {})
    if 'saved' in extension:
        old = portable.saved(value)
        return extension.get('details'), old['capture'], ''
    if 'legacy' in extension:
        record = extension['legacy']['record']
        snapshot = extension.get('details')
        if snapshot is not None:
            validate(snapshot, record)
        return snapshot, record, record.get('bundle_path', '')
    raise ValueError('Saved details context is unavailable.')


def read(root, item, forbidden=()):
    """Read one already validated save, including transferred legacy wrappers."""
    from services import item_manifest
    result = dict(captured=False, captured_at=None, description=None, description_available=False,
                  description_status='absent', like_count=None, dislike_count=None,
                  extractor=None, extractor_version=None)
    if not item.get('layout_version'):
        return result, None
    prefix = item.get('bundle_path', '')
    relative = '/'.join(p for p in (prefix, item_manifest.MANIFEST) if p)
    raw = item_manifest._metadata_bytes(portable_path(root, relative, forbidden))
    if hashlib.sha256(raw).hexdigest() != item['manifest_sha256']:
        raise ValueError('Saved metadata changed.')
    import json
    parsed=json.loads(raw,object_pairs_hook=item_manifest._unique)
    if not isinstance(parsed,dict) or parsed.get('format')!=item_manifest.FORMAT:
        return result,None
    value = item_manifest.decode(raw)
    snapshot, facts, nested = manifest_snapshot(value)
    if snapshot is None:
        return result, None
    validate(snapshot, facts)
    result.update(captured=True, **{key:snapshot[key] for key in
        ('captured_at','like_count','dislike_count','extractor','extractor_version','description_status')})
    content = None
    if snapshot['description'] is not None:
        folder = '/'.join(p for p in (prefix, nested) if p)
        try:
            directory = portable_path(root, folder, forbidden) if folder else Path(root)
            content = asset_bytes(directory, snapshot, forbidden)
            result.update(description=content.decode('utf-8'), description_available=True)
        except (OSError, ValueError, UnicodeError):
            result['description_status'] = 'unavailable'
        except ServeDenied as error:
            if error.status_code != 404:raise
            result['description_status'] = 'unavailable'
    # A changed manifest cannot supply even otherwise valid-looking counts.
    if item_manifest._metadata_bytes(portable_path(root, relative, forbidden)) != raw:
        raise ValueError('Saved metadata changed during reading.')
    return result, content

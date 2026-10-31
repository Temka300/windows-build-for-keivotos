"""Small, validated capture-time facts; never retain the engine's signed URLs."""
from datetime import datetime, timezone
import math
from pathlib import Path
import re

from modules.youtube.engine import canonical_url

CHANNEL_ID = re.compile(r'UC[A-Za-z0-9_-]{22}\Z')
COLUMNS = {
    'metadata_version': 'INTEGER NOT NULL DEFAULT 0',
    **{name: "TEXT NOT NULL DEFAULT ''" for name in (
        'video_id', 'channel_id', 'channel_name', 'channel_url', 'upload_date',
        'captured_at', 'media_ext', 'format_id', 'video_codec', 'audio_codec',
        'avatar', 'avatar_error')},
    'duration_seconds': 'REAL DEFAULT NULL',
}
ART_NAMES = {'.keivotos/channel-avatar.jpg', '.keivotos/channel-avatar.png', '.keivotos/channel-avatar.webp'}


def text(value, limit=512):
    return ''.join(c for c in ' '.join(value.split()) if ord(c) >= 32 and ord(c) != 127)[:limit] if isinstance(value, str) else ''


def channel_url(identity):
    return 'https://www.youtube.com/channel/' + identity if CHANNEL_ID.fullmatch(identity or '') else ''


def upload_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{8}', value):return ''
    try:datetime.strptime(value, '%Y%m%d')
    except ValueError:return ''
    return value


def duration(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 31536000 else None


def capture(info, url, media):
    expected = canonical_url(url).rsplit('=', 1)[-1]
    if not isinstance(info, dict) or info.get('id') != expected:
        raise ValueError('Source response does not match the requested video')
    identity = info.get('channel_id')
    identity = identity if isinstance(identity, str) and CHANNEL_ID.fullmatch(identity) else ''
    return dict(metadata_version=1, video_id=expected, channel_id=identity,
        channel_name=text(info.get('channel') or info.get('uploader')),
        channel_url=channel_url(identity), upload_date=upload_date(info.get('upload_date')),
        duration_seconds=duration(info.get('duration')),
        captured_at=datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
        media_ext=Path(media).suffix.lower().lstrip('.'), format_id=text(info.get('format_id'), 128),
        video_codec=text(info.get('vcodec'), 128), audio_codec=text(info.get('acodec'), 128),
        avatar='', avatar_error='Channel profile image was not supplied.' if identity else 'A reliable channel ID was not supplied.')


def validate(facts, url, media):
    """Recheck the helper boundary before publishing any files or personal facts."""
    if not isinstance(facts, dict) or set(facts) != set(COLUMNS) or type(facts['metadata_version']) is not int or facts['metadata_version'] != 1:
        raise ValueError('Invalid captured metadata')
    if facts['video_id'] != canonical_url(url).rsplit('=', 1)[-1]:
        raise ValueError('Captured video identity changed')
    for name in ('channel_name', 'format_id', 'video_codec', 'audio_codec', 'avatar_error'):
        maximum = 128 if name in ('format_id', 'video_codec', 'audio_codec') else 512
        if not isinstance(facts[name], str) or text(facts[name], maximum) != facts[name]:
            raise ValueError('Invalid captured text')
    identity = facts['channel_id']
    if not isinstance(identity, str) or identity and not CHANNEL_ID.fullmatch(identity):
        raise ValueError('Invalid captured channel identity')
    if facts['channel_url'] != channel_url(identity):raise ValueError('Captured channel URL changed')
    if facts['upload_date'] != upload_date(facts['upload_date']):raise ValueError('Invalid captured upload date')
    if facts['duration_seconds'] is not None and duration(facts['duration_seconds']) != facts['duration_seconds']:
        raise ValueError('Invalid captured duration')
    stamp = facts['captured_at']
    if not isinstance(stamp, str) or len(stamp) != 20:
        raise ValueError('Invalid capture time')
    try:datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError:raise ValueError('Invalid capture time') from None
    if facts['media_ext'] != Path(media).suffix.lower().lstrip('.') or not re.fullmatch(r'[a-z0-9]{1,12}', facts['media_ext']):
        raise ValueError('Captured output format changed')
    if not isinstance(facts['avatar'], str) or facts['avatar'] not in ART_NAMES | {''} or bool(facts['avatar']) == bool(facts['avatar_error']):
        raise ValueError('Invalid captured profile image')
    return dict(facts)


def preserved_capture(connection, legacy_id):
    """Read captured facts from a catalog-only connection; never serve paths."""
    from services import item_catalog
    item = item_catalog.item_for_legacy(connection, namespace='youtube.download', legacy_key=legacy_id)
    if item is None:
        return None
    if (item['owner'], item['item_type']) != ('youtube', 'saved-media'):
        raise ValueError('Preserved YouTube item identity changed')
    captured = item_catalog.read_capture(connection, item_id=item['id'], namespace='youtube.download', capture_key=legacy_id)
    if captured is None:
        return None
    payload = captured['payload']
    if captured['format_version'] != 1 or set(payload) != {'legacy_job_ids', 'record'}:
        raise ValueError('Unsupported preserved YouTube capture')
    owners, record = payload['legacy_job_ids'], payload['record']
    if (not isinstance(owners, list) or not 1 <= len(owners) <= 100
            or any(not isinstance(owner, str) or not owner or len(owner) > 4096 or '\0' in owner for owner in owners)
            or owners[0] != legacy_id or len(set(owners)) != len(owners)
            or not isinstance(record, dict) or record.get('id') != legacy_id):
        raise ValueError('Preserved YouTube capture relationships changed')
    return dict(item_id=item['id'], **captured)


def _preserved_asset(root, relative, forbidden_roots, *, optional=False, hash_bytes=True):
    """Observe safe current bytes, without asserting an unknown historical hash."""
    import hashlib
    import os
    import stat
    from files_base import serving
    from modules.youtube import layout

    serving.resolve_within_source(root, relative, forbidden_roots, must_exist=False)
    path = layout.resolve(root, relative)
    try:
        before = path.stat()
    except FileNotFoundError:
        if optional:
            return None
        raise ValueError('Preserved media is unavailable') from None
    if not stat.S_ISREG(before.st_mode) or before.st_size == 0:
        if optional:
            return None
        raise ValueError('Preserved asset is unavailable or empty')
    if not hash_bytes:
        return dict(path=relative, byte_size=before.st_size, sha256=None)
    def identity(value):
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns
    with path.open('rb') as stream:
        if identity(os.fstat(stream.fileno())) != identity(before):
            raise ValueError('Preserved asset changed during validation')
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if identity(os.fstat(stream.fileno())) != identity(before):
            raise ValueError('Preserved asset changed during validation')
    if identity(path.stat()) != identity(before):
        raise ValueError('Preserved asset changed during validation')
    return dict(path=relative, byte_size=before.st_size, sha256=digest)


def preserved_media(connection, legacy_id, *, source_id, source_root, forbidden_roots, hash_assets=True):
    """Explicit catalog-only validation against a caller-authorized current source.

    No runtime caller is switched here. Returned hashes observe current bytes;
    media hashes absent from historical manifests cannot authenticate old bytes.
    Runtime projections may skip media/art hashes; manifests stay fully checked.
    """
    if type(hash_assets) is not bool:
        raise ValueError('Asset hashing selection must be boolean')
    captured = preserved_capture(connection, legacy_id)
    if captured is None:
        return None
    record = captured['payload']['record']
    if not isinstance(source_id, str) or not source_id or record.get('source_id') != source_id:
        raise ValueError('Preserved media belongs to another registered source')
    if not isinstance(record.get('path'), str) or not record['path']:
        raise ValueError('Preserved media has no recorded output')
    if (not {'url', 'thumbnail', 'title'} <= record.keys()
            or any(not isinstance(record.get(name, ''), str) for name in ('url', 'thumbnail', 'avatar', 'title'))):
        raise ValueError('Invalid preserved media facts')
    canonical_url(record.get('url'))
    root = Path(source_root).resolve(strict=True)
    forbidden = tuple(forbidden_roots)
    owners = captured['payload']['legacy_job_ids']
    version = record.get('layout_version', 0)
    if type(version) is not int or version not in (0, 1, 2):
        raise ValueError('Unsupported preserved media layout')
    references = dict(media=record['path'])
    expected_manifest_hash=record.get('manifest_sha256')
    if version == 2:
        from services import item_manifest as portable
        from modules.youtube.portable import legacy_record
        relative='/'.join(p for p in (record.get('bundle_path',''),portable.MANIFEST) if p)
        value,_=portable.read(root,relative,forbidden=forbidden,check_assets=False)
        from storage_layout import portable_path
        import hashlib
        physical_digest=hashlib.sha256(portable._metadata_bytes(portable_path(root,relative,forbidden))).hexdigest()
        projected,legacy_owners=legacy_record(value,source_id,record.get('root',''),record.get('bundle_path',''),
                                             manifest_digest=physical_digest)
        if legacy_owners!=owners or any(record.get(key)!=expected for key,expected in projected.items()):
            raise ValueError('Portable legacy saved facts changed')
        extension=value['extensions']['youtube'];original=extension['legacy']['record']
        if original.get('layout_version',0)==1:
            from modules.youtube import saved_item
            bundle=record.get('bundle_path','')
            item_root=portable_path(root,bundle,forbidden) if bundle else root
            saved_item.read(item_root,original,set(legacy_owners),forbidden_roots=forbidden,
                            manifest_relative=extension['legacy_manifest'])
            references['manifest']='/'.join(p for p in (bundle,extension['legacy_manifest']) if p)
            expected_manifest_hash=original['manifest_sha256']
        else:references['manifest']=relative
    elif version == 1:
        from modules.youtube import saved_item
        if (not set(COLUMNS) <= record.keys() or record['metadata_version'] != 1
                or not isinstance(record.get('bundle_path'), str)
                or not isinstance(record.get('manifest_sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', record['manifest_sha256'])):
            raise ValueError('Incomplete preserved bundle facts')
        saved_item.read(root, record, set(owners), forbidden_roots=forbidden)
        references['manifest'] = '/'.join(p for p in (record['bundle_path'],'.keivotos/item.json') if p)
    else:
        parts = record['path'].split('/')
        if len(parts) != 3 or parts[0] != 'YouTube' or parts[1] not in owners:
            raise ValueError('Preserved flat media relationships changed')
        folder = '/'.join(parts[:2])
        for role, relative in (('thumbnail', record.get('thumbnail', '')), ('avatar', record.get('avatar', ''))):
            if relative and (not isinstance(relative, str) or not relative.startswith(folder + '/')
                    or (role == 'thumbnail' and relative.rsplit('/', 1)[0] != folder)
                    or (role == 'avatar' and relative[len(folder) + 1:] not in ART_NAMES)):
                raise ValueError('Preserved flat artwork relationships changed')
    references.update(thumbnail=record.get('thumbnail', ''), avatar=record.get('avatar', ''))
    assets = {role: _preserved_asset(root, relative, forbidden, optional=role in ('thumbnail', 'avatar'),
                                    hash_bytes=hash_assets or role == 'manifest')
              for role, relative in references.items() if relative}
    if version in (1,2) and assets['manifest']['sha256'] != expected_manifest_hash:
        raise ValueError('Saved metadata changed during validation')
    result=dict(item_id=captured['item_id'], record=dict(record), assets=assets)
    if version in (1,2):
        from modules.youtube import details
        from services import item_manifest
        from storage_layout import portable_path
        if version==1:
            import json
            raw=item_manifest._metadata_bytes(portable_path(root,references['manifest'],forbidden))
            value=json.loads(raw,object_pairs_hook=item_manifest._unique)
            if not isinstance(value,dict) or value.get('format')!=item_manifest.FORMAT:return result
            value=item_manifest.decode(raw)
        snapshot,facts,nested=details.manifest_snapshot(value)
        if snapshot is not None:
            details.validate(snapshot,facts);result['details']=snapshot
            base='/'.join(p for p in (record.get('bundle_path',''),nested) if p)
            relative=details.asset_path(snapshot,base)
            if relative:
                try:
                    declared=item_manifest.asset(relative,role='metadata',size=snapshot['description']['size'],
                        sha256=snapshot['description']['sha256'],media_type='text/plain',required=False)
                    observed=item_manifest.observe(root,declared,forbidden,hash_bytes=hash_assets)
                    assets['metadata']=dict(path=relative,byte_size=observed['size'],sha256=observed['sha256']) if observed else None
                except (OSError,ValueError,UnicodeError):assets['metadata']=None
    return result


def _validate_retirements(cursor):
    """Check retirement structure in SQLite, without loading hidden fact payloads.

    As with baseline membership, this is not a full hash audit of unselected
    captures. Writers validate canonical captures before appending retirement.
    """
    if cursor.execute("""SELECT 1 FROM kv_item_captures r
        LEFT JOIN kv_item_captures f ON f.item_id=r.item_id AND f.capture_key=r.capture_key
            AND f.namespace='youtube.download'
        LEFT JOIN kv_item_captures m ON m.item_id=r.item_id AND m.capture_key=r.capture_key
            AND m.namespace='youtube.library'
        LEFT JOIN kv_legacy_keys a ON a.item_id=r.item_id AND a.legacy_key=r.capture_key
            AND a.namespace='youtube.download'
        LEFT JOIN kv_items i ON i.id=r.item_id
        WHERE r.namespace='youtube.retirement' AND CASE
            WHEN NOT json_valid(r.payload_json) OR NOT json_valid(f.payload_json)
                OR NOT json_valid(m.payload_json) THEN 1
            ELSE r.format_version<>1 OR f.format_version<>1 OR a.item_id IS NULL OR i.owner IS NOT 'youtube'
                OR i.item_type IS NOT 'saved-media' OR m.format_version<>1
                OR json_type(r.payload_json) IS NOT 'object'
                OR (SELECT count(*) FROM json_each(r.payload_json))<>2
                OR json_type(r.payload_json,'$.legacy_id') IS NOT 'text'
                OR json_extract(r.payload_json,'$.legacy_id') IS NOT r.capture_key
                OR json_extract(f.payload_json,'$.record.id') IS NOT r.capture_key
                OR json_type(r.payload_json,'$.superseded_by') IS NOT 'text'
                OR length(json_extract(r.payload_json,'$.superseded_by')) NOT BETWEEN 1 AND 4096
                OR instr(json_extract(r.payload_json,'$.superseded_by'),char(0))>0
                OR json_extract(r.payload_json,'$.superseded_by')=r.capture_key
                OR json_extract(m.payload_json,'$.legacy_id') IS NOT r.capture_key
                OR coalesce(json_type(m.payload_json,'$.listed'),'') NOT IN ('true','false')
                OR json_type(m.payload_json,'$.superseded_by') IS NOT 'text'
                OR (json_extract(m.payload_json,'$.listed')=1)<>
                    (json_extract(m.payload_json,'$.superseded_by')='')
                OR (json_extract(m.payload_json,'$.listed')=0 AND
                    json_extract(m.payload_json,'$.superseded_by') IS NOT
                    json_extract(r.payload_json,'$.superseded_by')) END LIMIT 1""").fetchone():
        raise ValueError('Invalid saved retry retirement')


def _preserved_file_capture(connection, source_id, relative):
    """Find an exact owner relationship, then validate its immutable capture.

    SQLite filters candidates; no operational rows or filename inference are
    used. A derived reverse index can replace this query at the index boundary.
    """
    import sqlite3
    if not relative:
        return None
    if any(not isinstance(value, str) or not value or len(value) > 4096 or '\0' in value
           for value in (source_id, relative)):
        raise ValueError('Invalid preserved file relationship')
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    try:
        _validate_retirements(cursor)
        rows = cursor.execute("""SELECT item_id,capture_key FROM (
            SELECT f.item_id,f.capture_key,
                   CASE WHEN json_valid(f.payload_json) THEN f.payload_json ELSE NULL END AS payload
            FROM kv_item_captures f WHERE f.namespace='youtube.download' AND NOT EXISTS (
                SELECT 1 FROM kv_item_captures r WHERE r.namespace='youtube.retirement'
                    AND r.item_id=f.item_id AND r.capture_key=f.capture_key)
        ) WHERE json_extract(payload,'$.record.source_id')=? AND ? IN (
            json_extract(payload,'$.record.path'),json_extract(payload,'$.record.thumbnail'),
            json_extract(payload,'$.record.bundle_path')) LIMIT 2""", (source_id, relative)).fetchall()
    finally:
        cursor.close()
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError('Ambiguous preserved file relationship')
    selected = rows[0]
    captured = preserved_capture(connection, selected['capture_key'])
    if captured is None or captured['item_id'] != selected['item_id']:
        raise ValueError('Preserved file identity changed')
    record = captured['payload']['record']
    if any(not isinstance(record.get(name), str) for name in
           ('id', 'url', 'source_id', 'path', 'video', 'audio', 'title', 'thumbnail', 'created_at')):
        raise ValueError('Invalid preserved file facts')
    return captured


def file_metadata(context, *, use_catalog=False):
    """Exact retained download relationships, independent of operational state."""
    db = context.user_connection
    from files_base import serving, sources
    forbidden = [context.suite_home, *(Path(child.path) for child in sources.descendant_sources(context.source, context.registered))]
    if use_catalog:
        from services import item_catalog
        try:
            captured = _preserved_file_capture(db, context.source.source_id, context.relative_path)
        except ValueError:
            return {'status': 'unavailable'}
        if captured is None:
            return None
        if item_catalog.library_for_legacy(db, namespace='files.source', legacy_key=context.source.source_id) is None:
            return {'status': 'unavailable'}
        job = dict(captured['payload']['record'])
    else:
        columns = {row['name'] for row in db.execute("SELECT name FROM pragma_table_info('youtube_downloads')")}
        matches = [name for name in ('path', 'thumbnail', 'bundle_path') if name in columns]
        if not matches:
            return None
        rows = db.execute('SELECT * FROM youtube_downloads WHERE source_id=? AND status=\'complete\' AND (' +
            ' OR '.join(name + '=?' for name in matches) + ') LIMIT 2',
            [context.source.source_id, *([context.relative_path] * len(matches))]).fetchall()
        if not rows or not context.relative_path:
            return None
        if len(rows) != 1:
            return {'status': 'unavailable'}
        job = dict(rows[0])
    try:
        url = canonical_url(job['url'])
    except ValueError:
        return {'status': 'unavailable'}
    if job.get('layout_version')==2:
        if preserved_media(db,job['id'],source_id=context.source.source_id,source_root=context.source.path,
                           forbidden_roots=forbidden,hash_assets=False) is None:
            return {'status':'unavailable'}
    elif job.get('layout_version'):
        from modules.youtube import saved_item
        serving.resolve_served_file(context.source.path, '/'.join(p for p in (job['bundle_path'],'.keivotos/item.json') if p), forbidden)
        if use_catalog:
            owners = set(captured['payload']['legacy_job_ids'])
        else:
            # The manifest may belong to the first preserved attempt in a retry chain.
            owners = {job['id']}; previous = job
            for _ in range(100):
                if not previous.get('retry_of'):
                    break
                row = db.execute('SELECT * FROM youtube_downloads WHERE id=?', (previous['retry_of'],)).fetchone()
                if row is None or row['id'] in owners:
                    return {'status': 'unavailable'}
                previous = dict(row)
                if any(previous[key] != job[key] for key in ('source_id', 'url', 'video', 'audio')):
                    return {'status': 'unavailable'}
                owners.add(previous['id'])
            else:
                return {'status': 'unavailable'}
        if not saved_item.read(Path(context.source.path).resolve(), job, owners,
                               **({'forbidden_roots': forbidden} if use_catalog else {})):
            return {'status': 'unavailable'}
    fields = []
    def add(label, value, href=None):
        placement = 'properties' if label in ('File format', 'Format ID', 'Video codec', 'Audio codec', 'Duration', 'Saved file') else 'origin'
        fields.append({'label': label, 'value': str(value) if value is not None and value != '' else None,
                       'href': href, 'placement': placement})
    add('Source URL', url, url)
    add('Title', job.get('title'))
    add('Channel', job.get('channel_name'))
    add('Channel ID', job.get('channel_id'))
    channel = channel_url(job.get('channel_id'))
    add('Channel URL', channel, channel)
    add('Downloaded', job.get('captured_at'))
    add('Uploaded', job.get('upload_date'))
    add('Requested video', job.get('video'))
    add('Requested audio', job.get('audio'))
    add('File format', job.get('media_ext'))
    add('Format ID', job.get('format_id'))
    add('Video codec', job.get('video_codec'))
    add('Audio codec', job.get('audio_codec'))
    seconds = duration(job.get('duration_seconds'))
    add('Duration', f'{int(seconds)//60}:{int(seconds)%60:02d}' if seconds is not None else None)
    try:
        destination = serving.resolve_within_source(context.source.path, job['path'], forbidden)
        add('Saved file', str(destination) if destination.is_file() else None)
    except (serving.ServeDenied, OSError, ValueError):
        add('Saved file', None)
    return {'fields': fields, 'related': [{'label': label, 'path': job[name]}
        for label, name in [('Downloaded file', 'path'), ('Thumbnail', 'thumbnail')] if job.get(name)]}


def preserved_library(connection, q='', offset=0, limit=60):
    """List saved items with explicit retirement, without job history or files.

    Presentation defaults describe completed saves, never reconstructed jobs.
    This explicit reader does not replace the current mixed job/library API.
    """
    import sqlite3
    from services import item_catalog
    if (not isinstance(q, str) or len(q) > 256 or type(offset) is not int
            or not 0 <= offset <= 2**63 - 1 or type(limit) is not int or not 1 <= limit <= 120):
        raise ValueError('Invalid saved-library query')
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    try:
        _validate_retirements(cursor)
        if cursor.execute("""SELECT 1 FROM kv_item_captures
            WHERE namespace IN ('youtube.download','youtube.library') AND CASE
                WHEN NOT json_valid(payload_json) THEN 1
                WHEN namespace='youtube.download' THEN
                    json_type(payload_json,'$.record.title') IS NOT 'text'
                    OR json_type(payload_json,'$.record.created_at') IS NOT 'text'
                ELSE coalesce(json_type(payload_json,'$.listed'),'') NOT IN ('true','false')
                    OR json_type(payload_json,'$.legacy_id') IS NOT 'text'
                    OR json_type(payload_json,'$.superseded_by') IS NOT 'text'
                    OR (json_extract(payload_json,'$.listed')=1) <>
                        (json_extract(payload_json,'$.superseded_by')='') END LIMIT 1""").fetchone():
            raise ValueError('Invalid saved-library registration')
        if cursor.execute("""SELECT 1 FROM kv_item_captures f
            LEFT JOIN kv_item_captures m ON m.item_id=f.item_id AND m.capture_key=f.capture_key
                AND m.namespace='youtube.library'
            WHERE f.namespace='youtube.download' AND m.item_id IS NULL LIMIT 1""").fetchone():
            raise ValueError('Preserve saved-library membership before reading')
        rows = cursor.execute("""WITH captures AS (
            SELECT f.item_id,f.capture_key,
                CASE WHEN json_valid(f.payload_json) THEN f.payload_json ELSE NULL END AS facts,
                CASE WHEN json_valid(m.payload_json) THEN m.payload_json ELSE NULL END AS membership
            FROM kv_item_captures f JOIN kv_item_captures m
                ON m.item_id=f.item_id AND m.capture_key=f.capture_key AND m.namespace='youtube.library'
            WHERE f.namespace='youtube.download' AND NOT EXISTS (
                SELECT 1 FROM kv_item_captures r WHERE r.namespace='youtube.retirement'
                    AND r.item_id=f.item_id AND r.capture_key=f.capture_key)
        ), matching AS (
            SELECT item_id,capture_key,json_extract(facts,'$.record.created_at') AS created_at
            FROM captures WHERE json_extract(membership,'$.listed')=1
                AND instr(lower(json_extract(facts,'$.record.title')),lower(?))>0
        ), page AS (
            SELECT * FROM matching ORDER BY created_at DESC,capture_key LIMIT ? OFFSET ?
        ) SELECT totals.total,page.item_id,page.capture_key FROM (SELECT count(*) AS total FROM matching) totals
            LEFT JOIN page ON 1=1 ORDER BY page.created_at DESC,page.capture_key""", (q, limit, offset)).fetchall()
    finally:
        cursor.close()
    items = []
    for selected in rows:
        legacy_id = selected['capture_key']
        if legacy_id is None:
            continue
        captured = preserved_capture(connection, legacy_id)
        if captured is None or captured['item_id'] != selected['item_id']:
            raise ValueError('Preserved library item identity changed')
        membership = item_catalog.read_capture(connection, item_id=selected['item_id'],
            namespace='youtube.library', capture_key=legacy_id)
        expected = dict(legacy_id=legacy_id, listed=True, superseded_by='')
        if (membership is None or membership['format_version'] != 1
                or membership['payload'] != expected or type(membership['payload'].get('listed')) is not bool):
            raise ValueError('Preserved library membership changed')
        record = captured['payload']['record']
        if any(not isinstance(record.get(name), str) for name in
               ('id', 'url', 'source_id', 'path', 'video', 'audio', 'title', 'thumbnail', 'created_at')):
            raise ValueError('Invalid preserved library facts')
        item = {name: '' for name in COLUMNS}
        item.update(metadata_version=0, duration_seconds=None, layout_version=0, bundle_path='', manifest_sha256='', retry_of='')
        item.update(record)
        item.update(status='complete', progress=100, error='', failure_phase='', superseded_by='')
        item.pop('root', None)
        items.append(item)
    return dict(items=items, total=rows[0]['total'])


def log_preserved_playback_failure(connection, legacy_id, reason, *, suite_home):
    """Explicit job-independent diagnostics; existing API routing stays unchanged."""
    import logging
    from files_base import sources
    from services import item_catalog
    if (not isinstance(legacy_id, str) or not 1 <= len(legacy_id) <= 64
            or not isinstance(reason, str) or reason not in (
                'unsupported_format', 'network', 'decode', 'unsupported_codec', 'playback', 'fullscreen')):
        raise ValueError('Invalid playback diagnostic')
    captured = preserved_capture(connection, legacy_id)
    if captured is None:
        raise ValueError('Saved playback item is unavailable')
    record = captured['payload']['record']
    if not isinstance(record.get('source_id'), str) or not record['source_id']:
        raise ValueError('Saved playback source is unavailable')
    source = sources.get_source(connection, record['source_id'])
    if source is None or not source.visible or source.role not in ('files', 'video', 'youtube'):
        raise ValueError('Registered playback folder is unavailable')
    if item_catalog.library_for_legacy(connection, namespace='files.source', legacy_key=source.source_id) is None:
        raise ValueError('Saved playback library is unavailable')
    forbidden = [Path(suite_home), *(Path(child.path) for child in
                 sources.descendant_sources(source, sources.list_sources(connection)))]
    observed = preserved_media(connection, legacy_id, source_id=source.source_id,
        source_root=source.path, forbidden_roots=forbidden, hash_assets=False)
    if observed is None or observed['record'] != record:
        raise ValueError('Saved playback facts changed')
    logging.getLogger('keivotos.youtube').warning(
        'Playback failed: saved=%r source=%r file=%r reason=%s',
        legacy_id, source.source_id[:256], record['path'][:1024], reason,
        extra={'event_code':'playback.failed','library_id':source.source_id[:256],
               'event_details':{'owner':'youtube','reason':reason,'saved_id':legacy_id}})
    return {'status': 'logged'}

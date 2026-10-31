"""Passive module facts for a freshly guarded Files subject; no owner imports."""
import hashlib
import logging
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from files_base import index, serving, sources
from module_descriptor import FileMetadataContext

logger = logging.getLogger('keivotos.files')


def context(connection, db, suite_home, source_id, relative):
    registered = tuple(sources.list_sources(connection))
    source = next((item for item in registered if item.source_id == source_id and item.visible), None)
    if source is None:
        raise serving.ServeDenied(404, 'Folder is unavailable')
    forbidden = [Path(suite_home), *(Path(child.path) for child in sources.descendant_sources(source, registered))]
    target = serving.resolve_within_source(source.path, relative, forbidden)
    if not target.is_file() and not target.is_dir():
        raise serving.ServeDenied(404, 'File is unavailable')
    relative = target.relative_to(Path(source.path).resolve()).as_posix()
    relative = '' if relative == '.' else relative
    if relative:
        with index.open_index(db) as indexed:
            row = indexed.execute('SELECT 1 FROM files_index WHERE source_id=? AND path=? AND available=1',
                                  (source_id, str(target))).fetchone()
        if row is None:
            raise serving.ServeDenied(404, 'File is unavailable')
    return FileMetadataContext(source, registered, relative, target, Path(suite_home), Path(db), connection)


def _resolve(ctx, relative, must_exist=True):
    return serving.resolve_within_source(ctx.source.path, relative,
        [ctx.suite_home, *(Path(child.path) for child in sources.descendant_sources(ctx.source, ctx.registered))],
        must_exist=must_exist)


def _recheck(ctx):
    fresh = context(ctx.user_connection, ctx.index_path, ctx.suite_home, ctx.source.source_id, ctx.relative_path)
    if fresh.path != ctx.path:
        raise serving.ServeDenied(409, 'Folder changed while reading metadata')
    return fresh


def _link(value):
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password else None
    except ValueError:
        return None


def _facts(ctx, provider):
    result = provider(ctx)
    if result is None:
        return None
    fields = []
    for item in result.get('fields', [])[:32]:
        value = item.get('value')
        if value is not None and not isinstance(value, str):
            raise ValueError('Invalid passive metadata text')
        placement = 'properties' if item.get('placement') == 'properties' else 'origin'
        fields.append({'label': str(item['label'])[:128], 'value': value, 'placement': placement,
                       'href': _link(item.get('href')) if value and placement == 'origin' else None})
    related = []
    for item in result.get('related', [])[:32]:
        relative = item['path']
        try:
            target = _resolve(ctx, relative, must_exist=False)
            available = target.is_file()
        except (serving.ServeDenied, OSError, ValueError):
            continue
        related.append({'label': str(item['label'])[:128], 'path': relative, 'available': available})
    covers = []
    for relative in result.get('cover_paths', [])[:13]:
        try:
            candidate = _resolve(ctx, relative)
            stat = candidate.stat()
            if candidate.is_file():
                covers.append((relative, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        except (serving.ServeDenied, OSError, ValueError):
            continue
    version = hashlib.sha256(repr(covers).encode()).hexdigest()[:24] if covers else ''
    return {'status': result.get('status', 'ready'), 'fields': fields, 'related': related,
            'artwork': bool(covers), 'artwork_version': version}, covers


def read(ctx, registry):
    sections = []
    for owner, provider in registry.file_metadata_contributions():
        try:
            result = _facts(ctx, provider)
        except (OSError, ValueError, sqlite3.Error):
            logger.warning('Passive metadata unavailable: owner=%s', owner.slug, exc_info=True,
                           extra={'event_code':'metadata.unavailable','event_details':{'owner':owner.slug}})
            result = ({'status': 'unavailable', 'fields': [], 'related': [], 'artwork': False, 'artwork_version': ''}, [])
        if result is not None:
            sections.append({'owner': owner.slug, 'name': owner.name, **result[0]})
    _recheck(ctx)
    return {'sections': sections}


def artwork(ctx, registry, owner_id, size):
    owner = registry.get(owner_id)
    if owner is None or owner.file_metadata_provider is None:
        raise serving.ServeDenied(404, 'Local artwork is unavailable')
    result = _facts(ctx, owner.file_metadata_provider)
    if result:
        from routers.files import _thumbnail_cache_key
        from thumbnails import ensure_thumbnail
        for relative, *_ in result[1]:
            candidate = _resolve(ctx, relative)
            thumbnail = ensure_thumbnail(str(candidate), size, _thumbnail_cache_key(candidate))
            if thumbnail is not None and thumbnail.is_file():
                fresh = _recheck(ctx)
                _resolve(fresh, relative)
                return thumbnail
    raise serving.ServeDenied(404, 'Local artwork is unavailable')

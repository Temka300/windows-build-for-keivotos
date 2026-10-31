"""Music presentation facts and album grouping over the disposable Files index.

The compact catalog has at most one row per indexed audio file. It preserves
search/grouping facts independently of the smaller raw-tag cache; no media is
read here. Explicit metadata reads alone populate it. All paths remain Files'
identities, and fresh shared guards precede exposing facts or serving artwork.
"""
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re

from files_base import index, sources, serving
from services import audio_metadata
from modules.ost import library

_DISC = re.compile(r'^(?:cd|disc|disk)[\s._-]*0*(\d+)(?:\s*[-–:].*)?$', re.I)
_SCHEMA = '''CREATE TABLE IF NOT EXISTS ost_catalog (
    source_id TEXT NOT NULL, path TEXT NOT NULL, indexed_path TEXT NOT NULL,
    root TEXT NOT NULL, revision TEXT NOT NULL, identity TEXT NOT NULL DEFAULT '',
    facts TEXT NOT NULL, search_text TEXT NOT NULL, PRIMARY KEY(source_id,path));'''


def _text(values):
    return [value.strip() for value in values if isinstance(value, str) and value.strip()]


def _number(values):
    text = next(iter(_text(values)), '').split('/')[0]
    return int(text) if len(text) <= 6 and text.isdecimal() and 0 < int(text) < 1000000 else None


def presentation(name, metadata=None):
    metadata = metadata or audio_metadata.fallback(name)
    tags = metadata['tags']
    return {'title': metadata['display_title'], 'artists': _text(tags.get('artist', [])),
            'album': next(iter(_text(tags.get('album', []))), ''),
            'album_artists': _text(tags.get('albumartist', [])),
            'track': _number(tags.get('track', [])), 'disc': _number(tags.get('disc', [])),
            'duration': metadata['duration'], 'status': metadata['status']}


def _search(facts, fallback_album=''):
    return '\n'.join([facts['title'], *facts['artists'], facts['album'] or fallback_album, *facts['album_artists']]).casefold()


def synchronize(connection, owners, relative_path=None):
    """Derive membership/revisions from the existing index; never scan originals."""
    from services.catalog_index import is_combined, ensure_owner, IndexOwner
    if is_combined(connection):
        ensure_owner(connection, IndexOwner('ost', ('ost_catalog',),
                     initialize_index, rebuild_index, requires=('files',)))
    connection.executescript(_SCHEMA)
    if relative_path is None:
        connection.execute('DELETE FROM ost_catalog WHERE NOT EXISTS '
                           '(SELECT 1 FROM files_index i WHERE i.path=ost_catalog.indexed_path AND i.source_id=ost_catalog.source_id)')
    connection.create_function('music_fallback', 1, lambda name: json.dumps(presentation(name)))
    connection.create_function('music_fold', 2, lambda name, parent: (Path(name).stem+'\n'+PurePosixPath(_disc_parent(parent)).name).casefold())
    marks = ','.join('?' for _ in library.AUDIO_EXTENSIONS)
    for source in owners:
        connection.execute(
            'INSERT INTO ost_catalog(source_id,path,indexed_path,root,revision,facts,search_text) '
            f'SELECT source_id,relative_path,path,?,COALESCE(seen_at,\'\'),music_fallback(name),music_fold(name,parent) '
            f'FROM files_index WHERE source_id=? AND is_dir=0 AND ext IN ({marks}) '
            + ('AND relative_path=? ' if relative_path is not None else '') +
            'ON CONFLICT(source_id,path) DO UPDATE SET indexed_path=excluded.indexed_path,root=excluded.root,'
            'revision=excluded.revision,identity=\'\',facts=excluded.facts,search_text=excluded.search_text '
            'WHERE ost_catalog.indexed_path<>excluded.indexed_path '
            'OR ost_catalog.root<>excluded.root',
            [str(Path(source.path).resolve()), source.source_id, *library.AUDIO_EXTENSIONS,
             *([relative_path] if relative_path is not None else [])])
    connection.commit()


def _save(connection, source_id, path, identity, facts, root_name=''):
    connection.execute('UPDATE ost_catalog SET identity=?,facts=?,search_text=? WHERE source_id=? AND path=?',
                       (identity, json.dumps(facts), _search(facts, PurePosixPath(_disc_parent(_parent(path))).name or root_name), source_id, path))


def facts(connection, source, path, forbidden):
    fallback = presentation(PurePosixPath(path).name)
    try:
        _, _, identity = audio_metadata._snapshot(connection, source, path, forbidden)
        row = connection.execute('SELECT identity,facts FROM ost_catalog WHERE source_id=? AND path=?',
                                 (source.source_id, path)).fetchone()
        if row and row['identity'] == identity:
            try:
                result = json.loads(row['facts'])
                assert set(result) == set(fallback)
                assert isinstance(result['title'], str) and isinstance(result['album'], str)
                assert all(isinstance(result[key], list) and all(isinstance(value, str) for value in result[key])
                           for key in ('artists', 'album_artists'))
                assert all(result[key] is None or (type(result[key]) is int and 0 < result[key] < 1000000)
                           for key in ('disc', 'track'))
                duration = result['duration']
                assert duration is None or (type(duration) in (int, float) and math.isfinite(duration) and duration >= 0)
                assert result['status'] in ('ready', 'limited', 'invalid', 'unavailable', 'pending', 'busy', 'unsupported')
                return result
            except (ValueError, TypeError, KeyError, AssertionError):
                pass
        metadata = audio_metadata.cached(connection, source, path, forbidden)
        result = presentation(PurePosixPath(path).name, metadata)
        if metadata['status'] not in ('pending', 'busy') or row and row['identity']:
            _save(connection, source.source_id, path, identity if metadata['status'] not in ('pending', 'busy') else '', result, source.display_name)
        return result
    except (serving.ServeDenied, OSError, ValueError):
        fallback['status'] = 'unavailable'
        return fallback


def record(db, source, path, forbidden, result):
    """After an explicit read, retain a freshly validated compact presentation."""
    with index.open_index(db) as connection:
        # An explicit tag read updates one indexed identity. Full discovery
        # still reconciles deletions, but must not run once for every track.
        synchronize(connection, [source], path)
        current = audio_metadata.cached(connection, source, path, forbidden)
        if result['status'] not in ('pending', 'busy', 'unavailable') and current == result:
            _, _, identity = audio_metadata._snapshot(connection, source, path, forbidden)
            _save(connection, source.source_id, path, identity,
                  presentation(PurePosixPath(path).name, result), source.display_name)
        else:
            facts(connection, source, path, forbidden)
        connection.commit()


def _parent(path):
    parent = str(PurePosixPath(path).parent)
    return '' if parent == '.' else parent


def _disc_parent(parent):
    return _parent(parent) if _DISC.fullmatch(PurePosixPath(parent).name) else parent


def _tag_key(facts):
    return facts['album'].casefold(), tuple(sorted(artist.casefold() for artist in facts['album_artists']))


def group(rows, source_names):
    """Location first; only confirmed sibling discs may broaden that location."""
    evidence = {}
    for row in rows:
        item = row['music']; parent = row['parent']
        if parent and item['album'] and item['disc']:
            key = row['source_id'], _parent(parent), _tag_key(item)
            parents, discs = evidence.setdefault(key, (set(), set()))
            parents.add(parent); discs.add(item['disc'])
    result = {}
    for row in rows:
        item = row['music']; parent = row['parent']
        location = _disc_parent(parent)
        sibling = evidence.get((row['source_id'], _parent(parent), _tag_key(item)))
        if item['album'] and item['disc'] and sibling and len(sibling[0]) > 1 and len(sibling[1]) > 1:
            location = _parent(parent)
        row['album_location'] = location
        # Pending members keep one provisional folder album until its details
        # are known. Opening it warms only that candidate location first.
    pending_locations = {(row['source_id'], row['album_location']) for row in rows if row['music']['status'] in ('pending', 'busy')}
    for row in rows:
        item = row['music']; location = row['album_location']; owner = row['source_id']
        pending = (owner, location) in pending_locations
        tags = ('', ()) if pending else _tag_key(item)
        key = (owner, location, tags)
        album = result.setdefault(key, {'source_id': owner, 'location': location,
            'name': (item['album'] if not pending else '') or PurePosixPath(location).name or source_names[owner],
            'artists': item['album_artists'] if not pending else [], 'items': [], 'pending': pending})
        album['items'].append(row)
    for key, album in result.items():
        album['id'] = hashlib.sha256(json.dumps(key, ensure_ascii=True).encode()).hexdigest()[:32]
        album['items'].sort(key=lambda row: (
            row['music']['disc'] or (int(_DISC.fullmatch(PurePosixPath(row['parent']).name)[1])
                                    if _DISC.fullmatch(PurePosixPath(row['parent']).name) else 1),
            row['music']['track'] or 1000000, library._natural(row['name']),
            library._natural(row['parent']), row['path']))
        album['anchor'] = min((row['path'] for row in album['items']), key=library._natural)
    return sorted(result.values(), key=lambda album: (
        library._natural(album['name']), library._natural(source_names[album['source_id']]),
        library._natural(album['location']), album['id']))


def _rows(connection, registered, suite_home):
    owners = {source.source_id: source for source in registered if source.role == 'ost' and source.visible}
    synchronize(connection, owners.values())
    result = []
    marks = ','.join('?' for _ in library.AUDIO_EXTENSIONS)
    for source in owners.values():
        forbidden = library.forbidden_roots(source, registered, suite_home)
        excluded = [os.path.normcase(Path(os.path.relpath(path, source.path)).as_posix())
                    for path in forbidden if sources.is_strict_descendant_path(path, source.path)]
        for row in connection.execute(
                f'SELECT * FROM files_index WHERE source_id=? AND is_dir=0 AND available=1 AND ext IN ({marks})',
                [source.source_id, *library.AUDIO_EXTENSIONS]).fetchall():
            relative = row['relative_path']; normalized = os.path.normcase(relative)
            if any(normalized == child or normalized.startswith(child + os.path.normcase('/')) for child in excluded):
                continue
            result.append({'source_id': source.source_id, 'path': relative, 'parent': row['parent'],
                           'name': row['name'], 'ext': row['ext'], 'row': row,
                           'music': facts(connection, source, relative, forbidden)})
    connection.commit()
    return owners, result


def _card(connection, album, owners, registered, suite_home):
    source = owners[album['source_id']]
    forbidden = library.forbidden_roots(source, registered, suite_home)
    covers = [{'path': path, 'version': library.cover_version(connection, source, path, is_dir, forbidden)}
              for path, is_dir in [(album['anchor'], False), (album['location'], True)]]
    return {key: album[key] for key in ('id', 'source_id', 'name', 'artists', 'anchor', 'pending')} | {
        'covers': covers, 'total': len(album['items'])}


def listing(db, registered, suite_home, offset, limit, query=''):
    with index.open_index(db) as connection:
        owners, rows = _rows(connection, registered, suite_home)
        albums = group(rows, {key: source.display_name for key, source in owners.items()})
        if query:
            albums = [album for album in albums if query.casefold() in album['name'].casefold() or any(query.casefold() in _search(row['music']) for row in album['items'])]
        return {'items': [_card(connection, album, owners, registered, suite_home) for album in albums[offset:offset + limit]],
                'total': len(albums)}


def tracks(db, registered, suite_home, source_id, anchor, offset, limit, prepare=False):
    with index.open_index(db) as connection:
        owners, rows = _rows(connection, registered, suite_home)
        current = next((row for row in rows if row['source_id'] == source_id and row['path'] == anchor), None)
        if current is None:
            raise serving.ServeDenied(404, 'Album is unavailable')
        albums = group(rows, {key: source.display_name for key, source in owners.items()})
        album = next(album for album in albums if current in album['items'])
        selected = album['items']
        if prepare:
            location = album['location']
            if current['music']['album'] and current['music']['disc'] and current['parent']:
                location = _parent(current['parent'])
            selected = [row for row in rows if row['source_id'] == source_id and (
                _disc_parent(row['parent']) == location or row['parent'] == location or
                _parent(row['parent']) == location and (not row['music']['album'] or _tag_key(row['music']) == _tag_key(current['music'])))]
        source = owners[source_id]; forbidden = library.forbidden_roots(source, registered, suite_home)
        folders = {}; items = []
        divided = len({row['parent'] for row in album['items']}) > 1
        for row in selected[offset:offset + limit]:
            item = library.track_item(connection, source, row['row'], forbidden, folders)
            name = PurePosixPath(row['parent']).name
            item['disc_label'] = (name if name and not name.isdecimal() else f"Disc {row['music']['disc'] or 1}") if divided else ''
            items.append(item)
        connection.commit()
        return {'album': _card(connection, album, owners, registered, suite_home), 'items': items, 'total': len(selected)}


def initialize_index(connection):
    connection.executescript(_SCHEMA)


def rebuild_index(build):
    """Explicitly rebuild extracted presentation with the existing read engine."""
    owners = [source for source in build.sources if source.visible and source.role == 'ost']
    connection = build.connection
    synchronize(connection, owners)
    with_connection = connection.execute('PRAGMA database_list')
    path = Path(next(row[2] for row in with_connection if row[1] == 'main'))
    for source in owners:
        forbidden = library.forbidden_roots(source, build.sources, build.suite_home)
        after = ''
        while True:
            build.check()
            rows = connection.execute(
                'SELECT path FROM ost_catalog WHERE source_id=? AND path>? ORDER BY path LIMIT 64',
                (source.source_id, after)).fetchall()
            if not rows:
                break
            for row in rows:
                build.check()
                after = row['path']
                connection.commit()
                try:
                    result = audio_metadata.read(path, source, after, forbidden)
                    _, _, identity = audio_metadata._snapshot(connection, source, after, forbidden)
                    _save(connection, source.source_id, after, identity,
                          presentation(PurePosixPath(after).name, result), source.display_name)
                    connection.commit()
                except (OSError, serving.ServeDenied):
                    continue

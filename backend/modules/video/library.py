"""Read-only Video title/episode views of the shared Files index."""
from pathlib import Path, PurePosixPath
import re

from files_base import index, sources, serving
from modules.video import metadata


def natural(value):
    return ([(1, int(part)) if part.isdecimal() else (0, part.casefold())
             for part in re.split(r'(\d+)', value)], value)


def _guard(source, path, registered, forbidden, *, directory=False):
    excluded = [*forbidden, *(Path(child.path) for child in sources.descendant_sources(source, registered))]
    try:
        target = (serving.resolve_within_source if directory else serving.resolve_served_file)(
            source.path, path, excluded)
        return target.is_dir() if directory else True
    except (serving.ServeDenied, OSError, ValueError, RuntimeError):
        return False


def titles(db, registered, extensions, forbidden, *, user_db, categories, category=None, q='', source_id=None, title_path=None, kind=None, metadata_state=None):
    """Group filtered file identities without writing memberships or media.

    Top-level files are standalone titles. A top-level directory is a series;
    deeper directories are real section names, never guessed season numbers.
    Category membership remains attached to each existing video identity.
    """
    metadata_state=metadata_state or {'source':{},'personal':{}}
    registered = list(registered)
    owners = {source.source_id: source for source in registered if source.visible and source.role == 'video'
              and (source_id is None or source.source_id == source_id)}
    with index.open_index(db) as connection:
        scope, values = index.source_search_scope(connection, registered, source_ids=owners,
                                                 forbidden_paths=forbidden)
        columns = 'source_id,relative_path AS path,name,ext,size,mtime'
        where = f'{scope} AND is_dir=0 AND available=1'
        if title_path is not None:
            if kind == 'series':
                where += ' AND relative_path>=? AND relative_path<?'
                values.extend([title_path + '/', title_path + '0'])
            else:
                where += ' AND relative_path=?'
                values.append(title_path)
        marks = ','.join('?' for _ in extensions)
        media_where = f'{where} AND ext IN ({marks})'
        media_values = [*values, *extensions]
        if category is not None:
            connection.execute('ATTACH DATABASE ? AS category_user', (str(user_db),))
            predicate, parameters = categories.filter_sql(category, alias='files_index')
            media_where += ' AND ' + predicate
            media_values.extend(parameters)
        rows = connection.execute(f'SELECT {columns} FROM files_index WHERE {media_where}', media_values).fetchall()
        # Indexed local covers only: browsing never walks a directory or reads tags.
        images = connection.execute(
            f"SELECT {columns},parent FROM files_index WHERE {where} "
            "AND ext IN ('jpg','jpeg','png','webp','avif') "
            "AND lower(name) IN ('cover.jpg','cover.jpeg','cover.png','cover.webp','cover.avif',"
            "'folder.jpg','folder.jpeg','folder.png','folder.webp','folder.avif',"
            "'poster.jpg','poster.jpeg','poster.png','poster.webp','poster.avif')", values).fetchall()
    covers = {}
    for row in images:
        covers.setdefault((row['source_id'], row['parent']), []).append(dict(row))
    groups = {}
    term = q.strip().casefold()
    for row in rows:
        item = dict(row)
        source = owners.get(item['source_id'])
        if term and not metadata.matches(metadata_state,item['source_id'],item['path'],'video',term):
            continue
        if source is None or not _guard(source, item['path'], registered, forbidden):
            continue
        path = PurePosixPath(item['path'])
        kind, title_path = ('series', path.parts[0]) if len(path.parts) > 1 else ('video', item['path'])
        key = (item['source_id'], kind, title_path)
        if key not in groups:
            if kind == 'series' and not _guard(source, title_path, registered, forbidden, directory=True):
                continue
            groups[key] = {'source_id': item['source_id'], 'path': title_path, 'kind': kind,
                           'name': title_path if kind == 'series' else item['name'], 'episodes': []}
        section = str(path.parent.relative_to(title_path)) if kind == 'series' else ''
        groups[key]['episodes'].append(dict(item, section='' if section == '.' else section))
    result = []
    for group in groups.values():
        episodes = sorted(group['episodes'], key=lambda item: (natural(item['section']), natural(item['name']), item['path']))
        group['episodes'] = episodes
        source = owners[group['source_id']]
        cover = None
        if group['kind'] == 'series':
            candidates = sorted(covers.get((group['source_id'], group['path']), []),
                                key=lambda item: (('cover', 'folder', 'poster').index(PurePosixPath(item['name']).stem.lower()),
                                                  natural(item['name']), item['path']))
            for candidate in candidates:
                if _guard(source, candidate['path'], registered, forbidden):
                    cover = {key: value for key, value in candidate.items() if key != 'parent'}
                    break
        group.update(cover=cover or {key: value for key, value in episodes[0].items() if key != 'section'},
                     episode_count=len(episodes), size=sum(item['size'] for item in episodes),
                     mtime=max(item['mtime'] for item in episodes),
                     ext=episodes[0]['ext'] if group['kind'] == 'video' else '')
        result.append(group)
    return sorted(result, key=lambda item: (natural(item['name']), item['source_id'], item['kind'], item['path']))


def summaries(groups):
    return [{key: value for key, value in group.items() if key != 'episodes'} for group in groups]

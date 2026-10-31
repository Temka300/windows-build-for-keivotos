"""Danbooru source-evidence preservation and owner-bounded index rebuilding."""
from contextlib import closing, contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from services.catalog_index import (
    IndexOwner, _owned_manifest, _json, _tuple_cursor, quoted, preserve_owner,
)
from services.data_migrations import snapshot_user_database
from modules.danbooru.schema import ensure_catalog_index_schema

TABLES = ('files', 'posts', 'tags', 'post_tags', 'metadata', 'sync_manifest')


def index_owner(*, media_root=None, sidecar_dir=None):
    from functools import partial
    settings = tuple((key,str(Path(value).resolve())) for key,value in
                     (('media_root',media_root),('sidecar_dir',sidecar_dir)) if value is not None)
    observer = partial(source_observations,media_root,sidecar_dir) if settings else None
    return IndexOwner('danbooru', TABLES, ensure_catalog_index_schema,
                      partial(rebuild_index,media_root=media_root,sidecar_dir=sidecar_dir),
                      legacy_tables=('ingest_state','files_index','files_audio_metadata','ost_catalog'),
                      source_config=settings, observe=observer, preserve_result=True)

def rebuild_index(build, *, media_root=None, sidecar_dir=None):
    """Project preserved rows and explicitly selected local sidecars; never fetch."""
    import base64,hashlib,json
    from files_base import sources
    from storage_layout import LibraryRoot,deterministic_root_id,matching_root,load_library_roots,identity_for_media
    from modules.danbooru.pipeline import import_payload_to_sqlite,media_path_for_sidecar
    build.check()
    if media_root is not None:
        root=Path(media_root).resolve()
        with closing(_tuple_cursor(build.essential)) as cursor:
            user_path=Path(next(row[2] for row in cursor.execute('PRAGMA database_list') if row[1]=='main'))
        roots=load_library_roots(user_path)
        allowed=[LibraryRoot(deterministic_root_id(root),root.name,root),*roots]
        extra={str(value.path):value.name for value in roots}
        cache={}
        floor=referenced_file_floor(build.essential)
        for observation in build.observations.get('danbooru',[]):
            build.check()
            path=Path(observation['path'])
            raw=read_source_file(path)
            if hashlib.sha256(raw).hexdigest()!=observation['sha256']:
                raise ValueError('Danbooru sidecar changed during reconstruction')
            payload=json.loads(raw)
            marker=path.parent/'.keivotos/item.json'
            if marker.exists():
                from services import item_manifest as portable
                from modules.danbooru.portable import _payload
                value,_=portable.read(path.parent,forbidden=[build.suite_home])
                if value['item']['owner']=='danbooru':
                    if value['extensions'].get('danbooru',{}).get('source')!=path.name:
                        raise ValueError('Portable Danbooru sidecar relationship changed')
                    payload=_payload(value,path.parent,[build.suite_home])
            local=payload.get('local_file') or {}
            if not isinstance(local.get('path'),str):
                raise ValueError('Danbooru sidecar has no media identity')
            media=Path(local['path'])
            if sidecar_dir is not None:
                mapped=media_path_for_sidecar(path,'.danbooru.json',root,Path(sidecar_dir),library_roots=roots)
                if mapped is not None and mapped.resolve()!=media.resolve():
                    if media.exists():
                        raise ValueError('Conflicting Danbooru sidecar media identities')
                    media=mapped
                    local['path']=str(media)
            if matching_root(media,allowed) is None:
                raise ValueError('Danbooru sidecar references an unregistered root')
            enclosing=sources.nearest_ancestor_source(media,build.sources)
            if enclosing is not None and (not enclosing.visible or enclosing.role!='danbooru'):
                raise ValueError('Danbooru sidecar source authorization changed')
            root_id,relative=identity_for_media(media,root,roots)
            aliases=build.connection.execute(
                'SELECT id,path FROM files WHERE root_id=? AND relative_path=?',(root_id,str(relative))).fetchall()
            if len(aliases)>1:
                raise ValueError('Ambiguous preserved Danbooru root alias')
            if aliases and aliases[0][1]!=str(media):
                if referenced_file_path(build.essential,aliases[0][1]):
                    raise ValueError('Authored path requires coordinated relocation; index retained')
                conflict=build.connection.execute('SELECT id FROM files WHERE path=?',(str(media),)).fetchone()
                if conflict and conflict[0]!=aliases[0][0]:
                    raise ValueError('Conflicting preserved Danbooru media identity')
                build.connection.execute('UPDATE files SET path=? WHERE id=?',(str(media),aliases[0][0]))
            # Keep every original sidecar byte, including fields not projected by
            # today's parser, in the owner's already preserved metadata family.
            key='catalog-source:'+hashlib.sha256(str(path).encode()+b'\0'+raw).hexdigest()
            record=json.dumps({'path':str(path),'sha256':observation['sha256'],
                               'base64':base64.b64encode(raw).decode('ascii')},sort_keys=True)
            build.connection.execute('INSERT OR IGNORE INTO metadata(key,value) VALUES(?,?)',(key,record))
            prior=build.connection.execute('SELECT id,downloaded_at,local_md5 FROM files WHERE path=?',(str(media),)).fetchone()
            if prior:
                # Reconstruction never turns a partial/stale sidecar into a
                # refresh that erases preserved tags, hashes or raw source facts.
                if build.connection.execute('SELECT 1 FROM posts WHERE file_id=?',(prior[0],)).fetchone():
                    continue
                if prior[1]:local['downloaded_at']=prior[1]
                if prior[2]:
                    if local.get('md5') and local['md5']!=prior[2]:
                        raise ValueError('Conflicting preserved Danbooru content identity')
                    local['md5']=prior[2]
            import_payload_to_sqlite(build.connection,payload,root,cache,True,extra_roots=extra,
                                     library_roots=roots,preserve_unknown_dates=True,minimum_file_id=floor)
    from services import item_manifest as portable
    from modules.danbooru.portable import projection
    from files_base import sources
    from storage_layout import LibraryRoot
    known_sources={source.source_id:source for source in build.sources}
    for binding in portable.bindings(build.essential,owner='danbooru'):
        build.check()
        source=known_sources.get(binding['source_id'])
        if source is None or not source.visible or source.role!='danbooru':continue
        forbidden=[build.suite_home,*(Path(child.path) for child in sources.descendant_sources(source,build.sources))]
        value,_=portable.read(source.path,binding['path'],forbidden=forbidden)
        if (value['item']['id']!=binding['item_id'] or
                hashlib.sha256(portable.encode(value)).hexdigest()!=binding['digest']):
            raise ValueError('Portable Danbooru source identity changed during reconstruction')
        item_root=Path(source.path)/binding['root']
        payload=projection(value,item_root,forbidden)
        media=payload['local_file']['path']
        prior=build.connection.execute('SELECT id FROM files WHERE path=?',(media,)).fetchone()
        if prior and build.connection.execute('SELECT 1 FROM posts WHERE file_id=?',(prior[0],)).fetchone():continue
        import_payload_to_sqlite(build.connection,payload,Path(source.path),{},True,
            extra_roots={source.path:source.display_name},
            library_roots=[LibraryRoot(source.source_id,source.display_name,Path(source.path))],
            preserve_unknown_dates=True,minimum_file_id=referenced_file_floor(build.essential))
    with closing(_tuple_cursor(build.connection)) as cursor:
        if cursor.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Danbooru reference projection is inconsistent')

@dataclass(frozen=True)
class Replacement:
    original: Path
    candidate: Path
    recovery: Path
    expected: str | None
    file_id_floor: int = 0


def prepare_replacement(original, user_path=None):
    """Stage a rebuild beside its explicit output, retaining the complete old DB."""
    original = Path(original).absolute()
    if original.is_symlink() or original.resolve() != original:
        raise ValueError('Refused linked index rebuild target')
    original.parent.mkdir(parents=True, exist_ok=True)
    recovery = original.parent/(original.name+'.rebuild-'+uuid4().hex)
    recovery.mkdir()
    candidate = recovery/'candidate.sqlite'
    expected = None
    floor = 0
    if original.exists():
        snapshot = snapshot_user_database(original, recovery/'previous.sqlite')
        with closing(sqlite3.connect(snapshot.destination)) as source:
            expected = _json(_owned_manifest(source, TABLES))
            if user_path is not None:
                user_path = Path(user_path)
                if not user_path.is_file():
                    raise ValueError('Essential database is unavailable; index left unchanged')
                with closing(sqlite3.connect(user_path)) as essential:
                    preserve_owner(essential, source, index_owner())
                    floor=referenced_file_floor(essential)
                    essential.commit()
            with closing(sqlite3.connect(candidate)) as target:
                source.backup(target)
    return Replacement(original, candidate, recovery, expected, floor)


def install_replacement(replacement):
    """Install only Danbooru tables in one transaction; never unlink a live DB."""
    candidate, original = replacement.candidate, replacement.original
    if not original.exists():
        if replacement.expected is not None:
            raise ValueError('Original index disappeared; rebuild retained for review')
        with closing(sqlite3.connect(candidate)) as source:
            if source.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise ValueError('Rebuilt index failed integrity verification')
            source.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        os.link(candidate, original)
        return
    if replacement.expected is None:
        raise ValueError('An index appeared during rebuild; evidence retained')
    with closing(sqlite3.connect(original)) as target, closing(sqlite3.connect(candidate)) as source:
        source.execute('BEGIN')
        target.execute('PRAGMA journal_mode=WAL')
        target.execute('BEGIN IMMEDIATE')
        try:
            if _json(_owned_manifest(target,TABLES)) != replacement.expected:
                raise ValueError('Danbooru index changed during rebuild; old authority retained')
            definitions = [row for row in source.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END,name") if row[2] in TABLES]
            if any(row[0] not in ('table','index') for row in definitions):
                raise ValueError('Unsupported index schema; old authority retained')
            for table in reversed(TABLES):
                target.execute('DROP TABLE IF EXISTS '+quoted(table))
            for kind, name, table, sql in definitions:
                if kind == 'table':
                    target.execute(sql)
            for table in TABLES:
                fields = [row[1] for row in source.execute('PRAGMA table_info('+quoted(table)+')')]
                if not fields:
                    continue
                cursor = source.execute('SELECT * FROM '+quoted(table))
                while batch := cursor.fetchmany(64):
                    target.executemany('INSERT INTO '+quoted(table)+' VALUES('+','.join('?' for _ in fields)+')',batch)
            for kind, name, table, sql in definitions:
                if kind == 'index':
                    target.execute(sql)
            if source.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
                for name, sequence in source.execute('SELECT name,seq FROM sqlite_sequence'):
                    if name in TABLES:
                        target.execute('DELETE FROM sqlite_sequence WHERE name=?',(name,))
                        target.execute('INSERT INTO sqlite_sequence VALUES(?,?)',(name,sequence))
            if _owned_manifest(target,TABLES) != _owned_manifest(source,TABLES):
                raise ValueError('Rebuild installation verification failed')
            target.commit()
        except BaseException:
            target.rollback()
            raise


@contextmanager
def attempt_guard():
    """A supervised obsolete child cannot install an index generation."""
    import json
    from services import operations
    payload = os.environ.get('KEIVOTOS_DANBOORU_OPERATION')
    if not payload:
        yield
        return
    selected = json.loads(payload)
    path = Path(selected['jobs']).resolve(strict=True)
    with closing(sqlite3.connect(path)) as jobs:
        jobs.execute('BEGIN IMMEDIATE')
        operations.validate(jobs, 'danbooru', selected['id'], token=selected['token'])
        try:
            yield
        finally:
            jobs.rollback()


def read_source_file(path):
    if path.is_symlink():
        raise ValueError('Refused linked Danbooru source metadata')
    with path.open('rb') as handle:
        raw=handle.read(4*1024*1024+1)
    if len(raw)>4*1024*1024:
        raise ValueError('Danbooru source metadata is too large; original retained')
    return raw


def source_observations(media_root, sidecar_dir):
    import hashlib
    from modules.danbooru.pipeline import sidecar_files
    directories=[Path(value).resolve() for value in (media_root,sidecar_dir) if value is not None]
    files=sidecar_files(directories,'.danbooru.json')
    return [{'path':str(path.absolute()),'sha256':hashlib.sha256(read_source_file(path)).hexdigest()}
            for path in files]


def referenced_file_floor(essential):
    """Never reuse an unresolved personal file ID for unrelated new content."""
    highest=0
    with closing(_tuple_cursor(essential)) as cursor:
        for table in ('favorites','collection_items','user_image_tags','image_views'):
            columns={row[1] for row in cursor.execute('PRAGMA table_info('+quoted(table)+')')}
            if 'file_id' in columns:
                row=cursor.execute('SELECT max(file_id) FROM '+quoted(table)+
                                   " WHERE typeof(file_id)='integer'").fetchone()
                highest=max(highest,row[0] or 0)
    return highest



def referenced_file_path(essential,path):
    """Path-authored references cannot be silently rebound by index preparation."""
    with closing(_tuple_cursor(essential)) as cursor:
        for table in ('favorites','collection_items','user_image_tags','image_views'):
            columns={row[1] for row in cursor.execute('PRAGMA table_info('+quoted(table)+')')}
            if 'file_path' in columns and cursor.execute(
                    'SELECT 1 FROM '+quoted(table)+' WHERE file_path=? LIMIT 1',(path,)).fetchone():
                return True
    return False

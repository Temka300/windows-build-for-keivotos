"""danbooru selected collection and preservation ownership."""
from services.collection_transfer import TransferOwner

def contribution():
    from services.data_migrations import CatalogImporter
    from modules.danbooru.user_schema import import_catalog_records
    return TransferOwner('danbooru',catalog_importers=(CatalogImporter('danbooru.essential-records',1,import_catalog_records),),
        operational_table='ingest_state',operational_index=True,reference_files=references,
        select_records=select_records,source_records=source_records,metadata_files=metadata_files,import_metadata=import_metadata,import_source=import_source,personal_tables=PERSONAL_TABLES)


def references(db,configuration):
    from pathlib import Path
    import sqlite3
    from services.asset_preservation import Reference,_table
    if not _table(db,'artist_profile_assets'):return
    cursor=db.cursor();cursor.row_factory=sqlite3.Row
    try:
        for row in cursor.execute('SELECT id,file_path,content_hash FROM artist_profile_assets'):
            path=Path(row['file_path'])
            permitted=path.resolve().is_relative_to(Path(configuration.ARTIST_PROFILE_ARCHIVE_DIR).resolve())
            yield Reference('danbooru','profile:'+str(row['id']),path,'sha256',row['content_hash'],permitted=permitted)
    finally:cursor.close()


PERSONAL_TABLES=('favorites','collections','collection_items','user_image_tags','image_views','tag_removals',
                 'artist_profile_assets')


def source_records(selection,db):
    from services.collection_transfer import records_for_values,rows
    files=records_for_values(db,'files','id',selection.file_ids)
    # Root identities follow the current selected registration after import.
    for row in files:
        row['root_id']=selection.context.source.source_id
    posts=records_for_values(db,'posts','file_id',selection.file_ids)
    links=records_for_values(db,'post_tags','post_id',{r['id'] for r in posts})
    yield 'files',files
    yield 'posts',posts
    yield 'tags',records_for_values(db,'tags','id',{r['tag_id'] for r in links})
    yield 'post_tags',links
    # Raw source captures are associated by their declared media path, not by
    # arbitrary substring matches or all metadata belonging to this owner.
    import json,base64
    selected=set(selection.files)
    metadata=[]
    for row in rows(db,'metadata'):
        if row['key'].startswith('collection-source:'):
            value=json.loads(row['value'])
            if value.get('source_id')==selection.context.source.source_id and value.get('relative_path') in selected:
                metadata.append(row)
            continue
        if not row['key'].startswith('catalog-source:'):continue
        try:
            payload=json.loads(base64.b64decode(json.loads(row['value'])['base64']))
            path=__import__('pathlib').Path(payload['local_file']['path'])
            relative=path.relative_to(selection.context.source.path).as_posix()
        except (ValueError,KeyError,TypeError):continue
        if relative in selected:metadata.append(row)
    yield 'metadata',metadata


def select_records(selection):
    from services.collection_transfer import rows,records_for_values
    db=selection.context.connection
    for table in ('favorites','user_image_tags','image_views','tag_removals'):
        yield table,records_for_values(db,table,'file_id',selection.file_ids)
    members=records_for_values(db,'collection_items','file_id',selection.file_ids)
    yield 'collections',records_for_values(db,'collections','id',{r['collection_id'] for r in members})
    yield 'collection_items',members
    # Artwork is included only for artists actually present on selected posts.
    from contextlib import closing
    import sqlite3
    with closing(sqlite3.connect(selection.context.index_path)) as derived:
        posts=records_for_values(derived,'posts','file_id',selection.file_ids)
        links=records_for_values(derived,'post_tags','post_id',{r['id'] for r in posts})
        tags=records_for_values(derived,'tags','id',{r['tag_id'] for r in links})
    artists={r['name'] for r in tags if r['category']=='artist'}
    yield 'artist_profile_assets',records_for_values(db,'artist_profile_assets','tag_name',artists)


def metadata_files(selection,configuration):
    """Actual selected local sidecars, including canonical external stores."""
    if configuration is None or not hasattr(configuration,'SIDECAR_DIR'):return
    from pathlib import Path
    from storage_layout import sidecar_candidates,LibraryRoot,load_library_roots
    db=selection.context.connection
    cursor=db.cursor();cursor.row_factory=None
    try:path=Path(next(r[2] for r in cursor.execute('PRAGMA database_list') if r[1]=='main'))
    finally:cursor.close()
    roots=load_library_roots(path)
    source=selection.context.source
    if not any(Path(r.path)==Path(source.path) for r in roots):
        roots.append(LibraryRoot(source.source_id,source.display_name,Path(source.path)))
    for relative,media in selection.files.items():
        if media.suffix.casefold() in ('.json','.md','.txt'):continue
        for sidecar in sidecar_candidates(media,'.danbooru.json',Path(configuration.DATA_ROOT),
                                          Path(configuration.SIDECAR_DIR),roots):
            if sidecar.is_file():
                # A linked store is refused by the common file observation guard.
                yield relative,sidecar
                break


def import_metadata(db,essential,root,relative,raw,digest):
    import base64,json
    from pathlib import Path
    from storage_layout import LibraryRoot
    from modules.danbooru.pipeline import import_payload_to_sqlite
    from modules.danbooru.schema import ensure_catalog_index_schema
    from modules.danbooru.catalog_index import referenced_file_floor
    ensure_catalog_index_schema(db)
    payload=json.loads(raw)
    if not isinstance(payload,dict) or not isinstance(payload.get('local_file',{}),dict):
        raise ValueError('Invalid Danbooru source metadata')
    media=str(Path(root)/relative)
    projected={**payload,'local_file':{**payload.get('local_file',{}),'path':media}}
    prior=db.execute('SELECT id FROM files WHERE path=?',(media,)).fetchone()
    if not prior or not db.execute('SELECT 1 FROM posts WHERE file_id=?',(prior[0],)).fetchone():
        import_payload_to_sqlite(db,projected,Path(root),{},True,
            library_roots=[LibraryRoot(Path(root).name,Path(root).name,Path(root))],preserve_unknown_dates=True,
            minimum_file_id=referenced_file_floor(essential))
    capture=json.dumps({'path':media+'.danbooru.json','sha256':digest,'base64':base64.b64encode(raw).decode('ascii')},sort_keys=True)
    db.execute('INSERT OR IGNORE INTO metadata VALUES(?,?)',('catalog-source:'+digest,capture))
    associated=json.dumps({'source_id':Path(root).name,'relative_path':relative,'sha256':digest,
                           'base64':base64.b64encode(raw).decode('ascii')},sort_keys=True)
    db.execute('INSERT OR IGNORE INTO metadata VALUES(?,?)',('collection-source:'+Path(root).name+':'+digest,associated))


def import_source(db,source,location):
    from database import _ensure_column
    db.execute("CREATE TABLE IF NOT EXISTS registered_folders(name TEXT PRIMARY KEY,added_at TEXT NOT NULL DEFAULT(datetime('now')))")
    for name in ('path','root_id','display_name'):_ensure_column(db,'registered_folders',name,'TEXT')
    existing=db.execute('SELECT path,root_id FROM registered_folders WHERE name=?',(source['source_id'],)).fetchone()
    if existing is not None and tuple(existing)!=(str(location),source['source_id']):
        raise ValueError('Danbooru folder identity conflicts with this collection')
    db.execute('INSERT OR IGNORE INTO registered_folders(name,path,root_id,display_name,added_at) VALUES(?,?,?,?,?)',
               (source['source_id'],str(location),source['source_id'],source['display_name'],source['added_at']))

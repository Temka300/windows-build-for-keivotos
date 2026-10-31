"""Managed gallery-dl publication and explicit portable Danbooru projections."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid5, NAMESPACE_URL

from services import item_manifest as portable
from storage_layout import portable_path, identity_for_media, load_library_roots
from files_base import sources, serving

SOURCE_LIMIT=4*1024*1024

def fence(operation):
    from services import operations
    with closing(sqlite3.connect(Path(operation['jobs']).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        operations.validate(db,'danbooru',operation['id'],token=operation['token'])

def authorization(args,destination):
    path=Path(args.user_db)
    if not path.is_file():return None
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='files_sources'").fetchone():return None
        registered=sources.list_sources(db)
        source=sources.nearest_ancestor_source(destination,registered)
        if source is None:return None
        if not source.visible or source.role!='danbooru':
            raise ValueError('Danbooru destination ownership is unavailable')
        serving.resolve_within_source(source.path,Path(destination).relative_to(source.path).as_posix(),
            [Path(child.path) for child in sources.descendant_sources(source,registered)],must_exist=False)
        return {'source_id':source.source_id,'path':source.path,'role':source.role}

def staging(args,destination,operation):
    fence(operation)
    root=Path(destination).absolute()
    if root.resolve()!=root:raise ValueError('Managed destination contains links')
    root.mkdir(parents=True,exist_ok=True)
    stage=portable_path(root,'.keivotos/danbooru/'+operation['id'],(),must_exist=False)
    stage.mkdir(parents=True,exist_ok=True)
    authority=authorization(args,root)
    library_id=None
    if authority is not None:
        with closing(sqlite3.connect(args.user_db)) as db:
            db.row_factory=sqlite3.Row
            source=sources.get_source(db,authority['source_id'])
            library_id=portable.collection_identity(db,source)
    portable._create(stage/'intent.json',portable.encode({'destination':str(root),'operation':operation['id'],
        'authorization':authority,'library_id':library_id}))
    output=portable_path(stage,'downloads',(),must_exist=False);output.mkdir(exist_ok=True)
    return output

def _payload(value,root,forbidden=(),*,hash_media=False):
    from modules.danbooru import pipeline
    extension=value['extensions'].get('danbooru')
    if not isinstance(extension,dict) or set(extension)!={'source','media'}:
        raise ValueError('Missing Danbooru source references')
    source=portable_path(root,extension['source'],forbidden)
    declared=[a for a in value['assets'] if a['path']==extension['source'] and a['role']=='source']
    if len(declared)!=1:raise ValueError('Missing captured source asset')
    portable.observe(root,declared[0],forbidden)
    with source.open('rb') as stream:raw=stream.read(SOURCE_LIMIT+1)
    if len(raw)>SOURCE_LIMIT:raise ValueError('Danbooru source metadata exceeds its limit')
    facts=json.loads(raw,object_pairs_hook=portable._unique)
    if not isinstance(facts,dict):raise ValueError('Danbooru source facts are invalid')
    media=portable_path(root,extension['media'],forbidden)
    post=facts.get('post',facts)
    if not isinstance(post,dict) or type(post.get('id')) is not int or post['id']<=0:
        raise ValueError('Missing Danbooru post identity')
    local=facts.get('local_file') or {}
    known=local.get('md5') if isinstance(local,dict) else None
    payload=pipeline.build_payload(media,post,'gallery_dl',str(post.get('md5') or ''),
        local_md5=None if hash_media else known,observe_missing_hashes=hash_media)
    payload['local_file']['downloaded_at']=value['origin'].get('downloaded_at')
    return payload

def publish_download(args,destination,stage,operation):
    from services import operations
    from modules.danbooru import pipeline
    fence(operation)
    with (stage.parent/'intent.json').open('rb') as stream:
        intent=json.loads(stream.read(portable.MAX_BYTES+1),object_pairs_hook=portable._unique)
    def accepted():
        fence(operation)
        if intent.get('authorization')!=authorization(args,destination):
            raise ValueError('Destination authorization changed; staging retained')
    accepted()
    roots=load_library_roots(args.user_db)
    published=[]
    for media in pipeline.media_files([stage]):
        raw=media.with_name(media.name+args.json_suffix)
        if not raw.is_file():raise ValueError('Incomplete gallery-dl source facts; staging retained')
        portable_path(stage,raw.relative_to(stage).as_posix())
        with raw.open('rb') as stream:data=stream.read(SOURCE_LIMIT+1)
        if len(data)>SOURCE_LIMIT:raise ValueError('Danbooru source metadata exceeds its limit')
        post=json.loads(data,object_pairs_hook=portable._unique)
        if isinstance(post,dict) and 'post' in post:post=post['post']
        if not isinstance(post,dict) or type(post.get('id')) is not int or post['id']<=0:
            raise ValueError('Downloaded source has no post identity')
        media_info=portable.observe(stage,portable.asset(media.relative_to(stage).as_posix()))
        digest=media_info['sha256']
        identity=str(uuid5(NAMESPACE_URL,'urn:keivotos:danbooru:managed:'+str(post['id'])+':'+digest))
        root_id,_=identity_for_media(Path(destination),args.root,roots)
        root_id=intent.get('library_id') or root_id
        final=portable_path(Path(destination),str(post['id'])+'-'+identity[:8],(),must_exist=False)
        if final.exists():
            prior,_=portable.read(final,hash_assets=True)
            expected={a['role']:a for a in prior['assets']}
            if prior['item']['id']!=identity or set(expected)!={'media','source'}:
                raise ValueError('Completed Danbooru identity conflicts')
            for role,path in [('media',media),('source',raw)]:
                a=expected[role]
                if a['path']!=path.name:raise ValueError('Completed Danbooru asset conflicts')
                portable.observe(path.parent,{**a,'path':path.name})
            accepted()
            published.append(final)
            continue
        item_stage=stage.parent/('item-'+identity);item_stage.mkdir(exist_ok=True)
        assets=[]
        for role,path in [('media',media),('source',raw)]:
            target=portable_path(item_stage,path.name,(),must_exist=False)
            original=portable.observe(stage,portable.asset(path.relative_to(stage).as_posix()))
            portable.copy_observation(original,target)
            a=portable.stable_asset(identity,path.name,role=role)
            observed=portable.observe(item_stage,a)
            a.update(size=observed['size'],sha256=observed['sha256']);assets.append(a)
        # Capture completion facts once; a recovery retry reuses the exact bytes.
        marker=item_stage/portable.MANIFEST
        if marker.exists():value=portable.read(item_stage,hash_assets=True)[0]
        else:
            value=portable.new(item_id=identity,owner='danbooru',item_type='post-image',
                library_id=root_id,library_name=Path(destination).name,assets=assets,
                origin={'url':'https://danbooru.donmai.us/posts/'+str(post['id']),
                        'downloaded_at':datetime.now(timezone.utc).isoformat().replace('+00:00','Z')},
                extensions={'danbooru':{'source':raw.name,'media':media.name}})
            portable.write(item_stage,value)
        portable.read(item_stage,hash_assets=True)
        _payload(value,item_stage)
        from services import download_history
        with closing(sqlite3.connect(operation['jobs'])) as history_db:
            with operations.bind('danbooru',operation['id'],operation['token']):
                operations.validate(history_db,'danbooru',operation['id'])
                download_history.ensure_schema(history_db)
                download_history.link(history_db,'danbooru','portable:'+value['item']['id'],previous=operation['id'])
                history_db.commit()
        accepted()
        final=portable_path(Path(destination),str(post['id'])+'-'+identity[:8],(),must_exist=False)
        if final.exists():
            prior,_=portable.read(final,hash_assets=True)
            if prior!=value:raise ValueError('Completed Danbooru item conflicts; files retained')
        else:
            # Generated staging only. No existing flat/linked item is moved.
            item_stage.rename(final)
            portable._flush_directory(final.parent)
            portable._flush_directory(item_stage.parent)
        published.append(final)
    return published

def describe(context,selection,options):
    if not options:
        existing=portable.existing_description(context,selection,'danbooru')
        if existing is not None:return existing
    path=(Path(selection)/portable.MANIFEST).as_posix()
    if context.resolve(path,must_exist=False).exists():
        if options:raise ValueError('Managed item already has its captured context')
        value,_=portable.read(context.source.path,path,forbidden=context.forbidden)
        if value['item']['owner']!='danbooru':raise ValueError('Wrong portable owner')
        return value,context.resolve(selection)
    # A source sidecar is a preserved original, not a small SQLite fact blob.
    from modules.danbooru import pipeline
    import config
    media=context.resolve(selection)
    candidate=pipeline.existing_metadata_path_for(media,'.danbooru.json',config.DATA_ROOT,config.SIDECAR_DIR)
    value,_=portable.local_description(context,selection,options,owner='danbooru',item_type='post-image')
    if candidate is None:return value,Path(context.source.path)
    sidecar_root=Path(config.SIDECAR_DIR).absolute()
    if candidate.is_relative_to(sidecar_root):
        relative=candidate.relative_to(sidecar_root).as_posix()
        portable_path(sidecar_root,relative)
        source_root,source_relative,forbidden=sidecar_root,relative,()
    else:
        source_root=Path(context.source.path)
        source_relative=candidate.relative_to(source_root).as_posix()
        context.resolve(source_relative)
        forbidden=context.forbidden
    selected_source=portable_path(source_root,source_relative,forbidden)
    with selected_source.open('rb') as stream:raw=stream.read(SOURCE_LIMIT+1)
    if len(raw)>SOURCE_LIMIT:raise ValueError('Danbooru source metadata exceeds its limit')
    _source_post(raw)
    name='source/'+candidate.name
    value['assets'].append(portable.stable_asset(value['item']['id'],name,role='source',media_type='application/json'))
    value['extensions']['danbooru']={'source':name,'media':selection}
    selected={a['path']:(Path(context.source.path),a['path'],context.forbidden) for a in value['assets'] if a['role']!='source'}
    selected[name]=(source_root,source_relative,forbidden)
    return portable.validate(value),portable.AssetRoots(selected)

def register(value,context,relative):
    root_relative=Path(relative).parent.parent.as_posix()
    root=context.resolve('' if root_relative=='.' else root_relative)
    from modules.danbooru import pipeline
    payload=projection(value,root,context.forbidden,hash_media=True)
    from modules.danbooru.schema import ensure_catalog_index_schema
    target=context.registry.require('danbooru').database
    from services.catalog_index import is_combined
    if context.index_path.is_file():
        with closing(sqlite3.connect(context.index_path.resolve().as_uri()+'?mode=ro',uri=True)) as selected:
            if is_combined(selected):
                if not selected.execute("SELECT 1 FROM kv_index_owners WHERE owner='danbooru'").fetchone():
                    raise ValueError('Danbooru is not selected in the combined index')
                target=context.index_path
    target.parent.mkdir(parents=True,exist_ok=True)
    cursor=context.connection.cursor();cursor.row_factory=None
    try:user_path=cursor.execute("PRAGMA database_list").fetchone()[2]
    finally:cursor.close()
    with closing(sqlite3.connect(target)) as db:
        ensure_catalog_index_schema(db)
        prior=db.execute('''SELECT p.danbooru_post_id FROM files f JOIN posts p ON p.file_id=f.id
            WHERE f.path=?''',(payload['local_file']['path'],)).fetchone()
        # A preserved old capture must not replace newer retained source facts.
        if prior is not None and prior[0] is not None:return
        pipeline.import_payload_to_sqlite(db,payload,Path(context.source.path),{},True,
            extra_roots={context.source.path:context.source.display_name},
            library_roots=load_library_roots(Path(user_path)) if user_path else [],
            preserve_unknown_dates=True)
        db.commit()

def contribution():
    return portable.PortableOwner('danbooru',describe,register)


def register_download(args,published,operation):
    import config
    from files_base import index
    from services import item_catalog,operations
    fence(operation)
    registered_count=0
    if not Path(args.user_db).is_file():return 0
    with closing(sqlite3.connect(args.user_db)) as db:
        db.row_factory=sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='files_sources'").fetchone():return 0
        item_catalog.ensure_catalog_schema(db)
        registered=tuple(sources.list_sources(db))
        for directory in published:
            source=sources.nearest_ancestor_source(directory,registered)
            if source is None:continue
            if not source.visible or source.role!='danbooru':
                raise ValueError('Published Danbooru source is unavailable; output retained')
            relative=(directory/portable.MANIFEST).relative_to(source.path).as_posix()
            context=portable.Context(source,registered,config.SUITE_HOME,config.FILES_DB_PATH,db,config.MODULE_REGISTRY)
            with index.open_index(context.index_path) as connection:
                index.scan_source(connection,source.source_id,Path(source.path),excluded_roots=context.forbidden)
            db.execute('BEGIN IMMEDIATE')
            try:
                portable.import_item(context,relative)
                fence(operation)
                db.commit()
            except BaseException:
                db.rollback();raise
            registered_count+=1
    with closing(sqlite3.connect(operation['jobs'])) as state:
        with operations.bind('danbooru',operation['id'],operation['token']):
            operations.transition(state,'danbooru',operation['id'],
                publication='catalog-registered' if registered_count==len(published) else 'output-published')
    return registered_count


def _source_post(raw):
    facts=json.loads(raw,object_pairs_hook=portable._unique)
    if not isinstance(facts,dict):raise ValueError('Danbooru source facts are invalid')
    post=facts.get('post',facts)
    if not isinstance(post,dict) or type(post.get('id')) is not int or post['id']<=0:
        raise ValueError('Missing Danbooru post identity')
    return post


def projection(value,root,forbidden=(),*,hash_media=False):
    from modules.danbooru import pipeline
    if 'source' in value['extensions'].get('danbooru',{}):
        return _payload(value,root,forbidden,hash_media=hash_media)
    media=[a for a in value['assets'] if a['role']=='media']
    if len(media)!=1:raise ValueError('Select one local Danbooru image')
    target=portable_path(root,media[0]['path'],forbidden)
    payload=pipeline.build_payload(target,{},'portable-local','',observe_missing_hashes=hash_media)
    payload['post_url']=None
    payload['local_file']['downloaded_at']=value['origin'].get('downloaded_at')
    return payload

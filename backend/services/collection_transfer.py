"""Explicit collection copies and owner-qualified associated personal records."""
from services.operation_diagnostics import observed
from dataclasses import dataclass,replace
from typing import Callable

@dataclass(frozen=True)
class TransferOwner:
    slug: str
    catalog_importers: tuple = ()
    operational_table: str = ''
    preserve_operational: Callable | None = None
    operational_index: bool = False
    personal_tables: tuple = ()
    reference_files: Callable | None = None
    select_records: Callable | None = None
    source_records: Callable | None = None
    metadata_files: Callable | None = None
    import_metadata: Callable | None = None
    import_source: Callable | None = None
    content_namespaces: tuple = ()


from contextlib import closing,ExitStack
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4
from services import item_manifest as portable,item_catalog
from services.catalog_index import quoted,_pack,_unpack
from storage_layout import portable_path,portable_relative
from files_base import sources,index,serving

FORMAT='keivotos.collection'
VERSION=1
MAX_METADATA=128*1024*1024
CATALOG_TABLES=('kv_libraries','kv_library_sources','kv_items','kv_legacy_keys','kv_assets',
                'kv_item_assets','kv_item_relations','kv_item_captures')
PACKET_SCHEMA="""PRAGMA user_version=1;
CREATE TABLE sources(source_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE files(source_id TEXT NOT NULL,path TEXT NOT NULL,size INTEGER NOT NULL,
 sha256 TEXT NOT NULL,md5 TEXT NOT NULL,PRIMARY KEY(source_id,path));
CREATE TABLE records(owner TEXT NOT NULL,kind TEXT NOT NULL,table_name TEXT NOT NULL,
 columns_json TEXT NOT NULL,keys_json TEXT NOT NULL,row_json TEXT NOT NULL,
 UNIQUE(owner,kind,table_name,row_json));
CREATE TABLE metadata(owner TEXT NOT NULL,source_id TEXT NOT NULL,subject TEXT NOT NULL,
 path TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(owner,source_id,subject));
"""
IMPORT_SCHEMA="""CREATE TABLE IF NOT EXISTS kv_collection_imports(
 request_id TEXT PRIMARY KEY,packet_sha256 TEXT NOT NULL,destination TEXT NOT NULL,
 sources_json TEXT NOT NULL,include_personal INTEGER NOT NULL)"""


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':'))


def _table(db,name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone() is not None


def rows(db,table,where='1',parameters=()):
    """Owner code supplies table/query syntax; packet/request input never does."""
    if not _table(db,table):return []
    cursor=db.cursor();cursor.row_factory=sqlite3.Row
    try:return [dict(row) for row in cursor.execute('SELECT * FROM '+quoted(table)+' WHERE '+where,parameters)]
    finally:cursor.close()


def records_for_values(db,table,column,values):
    values=tuple(values)
    if not values:return []
    result=[]
    for offset in range(0,len(values),200):
        selected=values[offset:offset+200]
        result.extend(rows(db,table,quoted(column)+' IN ('+','.join('?' for _ in selected)+')',selected))
    return result


def _metadata(context,relative):
    target=context.resolve(relative)
    raw=portable._metadata_bytes(target)
    value=json.loads(raw,object_pairs_hook=portable._unique)
    if value.get('format')==portable.FORMAT:
        value,_=portable.read(context.source.path,relative,forbidden=context.forbidden)
        if value['item']['owner'] not in ('files',context.source.role):raise ValueError('Copied item owner changed')
        folder=Path(relative).parent.parent.as_posix()
        extra=['/'.join(p for p in ('' if folder=='.' else folder,a['path']) if p) for a in value['assets']]
    elif context.source.role=='youtube' and value.get('version')==1:
        # Existing legacy owner facts are transported byte-identically.
        if set(value)!={'version','saved_item_id','source_url','title','media','thumbnail','capture'}:
            raise ValueError('Invalid legacy saved metadata')
        folder=Path(relative).parent.parent.as_posix()
        extra=['/'.join((folder,name)) for name in
               (value['media'],value['thumbnail'],value['capture'].get('avatar')) if name]
    else:raise ValueError('Unknown completed item metadata')
    return [relative,*extra]


def inventory(context):
    """Current guarded files; never enter staging or registered child roots."""
    root=context.resolve('');found={}
    for directory,dirs,files in os.walk(root,followlinks=False):
        base=Path(directory);admitted=[]
        for name in dirs:
            child=base/name
            if name=='.keivotos':
                marker=child/'item.json'
                if marker.is_file():
                    for relative in _metadata(context,marker.relative_to(root).as_posix()):
                        target=context.resolve(relative,must_exist=False)
                        if target.is_file():found[relative]=target
                continue
            relative=child.relative_to(root).as_posix()
            try:context.resolve(relative)
            except (ValueError,OSError,serving.ServeDenied):continue
            if name in ('.git','.svn'):continue
            admitted.append(name)
        dirs[:]=admitted
        for name in files:
            if name.endswith(('.part','.partial')):continue
            target=base/name;relative=target.relative_to(root).as_posix()
            context.resolve(relative)
            if not target.is_file():continue
            found[relative]=target
    return dict(sorted(found.items()))


def _write_rows(packet,source,owner,kind,table,values):
    if not values:return
    cursor=source.cursor();cursor.row_factory=None
    try:definition=list(cursor.execute('PRAGMA table_info('+quoted(table)+')'))
    finally:cursor.close()
    columns=[row[1] for row in definition]
    keys=[row[1] for row in sorted(definition,key=lambda row:row[5]) if row[5]]
    if not keys:raise ValueError('Transfer records need a stable primary key: '+table)
    for value in values:
        if set(value)!=set(columns):raise ValueError('Transfer record fields changed')
        raw=_pack([value[name] for name in columns])
        if len(raw.encode())>16*1024*1024:raise ValueError('An associated record exceeds its transfer limit')
        packet.execute('INSERT OR IGNORE INTO records VALUES(?,?,?,?,?,?)',
            (owner,kind,table,_json(columns),_json(keys),raw))


def _catalog_records(context,packet,include_personal=False):
    db=context.connection;source_id=context.source.source_id
    if not _table(db,'kv_item_captures'):return
    content={'portable.manifest','portable.location'}
    for descriptor in context.registry:
        if descriptor.transfer_provider:content.update(descriptor.transfer_provider().content_namespaces)
    direct=set()
    current_files=inventory(context)
    for row in rows(db,'kv_item_captures'):
        if not include_personal and row['namespace'] not in content:continue
        payload=json.loads(row['payload_json'])
        record=payload.get('record',payload)
        if payload.get('source_id')!=source_id and record.get('source_id')!=source_id:continue
        relative=record.get('path',record.get('relative_path'))
        if row['namespace'] in content:
            if not isinstance(relative,str) or not selected_path(context,relative,current_files):continue
        elif isinstance(relative,str) and not admitted_path(context,relative):continue
        direct.add(row['item_id'])
    selected=set(direct)
    # Keep real parent context without unrelated descendants.
    relations=rows(db,'kv_item_relations')
    while True:
        parents={r['target_item_id'] for r in relations if r['source_item_id'] in selected}
        if parents<=selected:break
        selected.update(parents)
    links=[r for r in rows(db,'kv_item_assets') if r['item_id'] in direct]
    assets={r['asset_id'] for r in links}
    library=item_catalog.library_for_legacy(db,namespace='files.source',legacy_key=source_id)
    library_ids={library['id']} if library else set()
    captures=[r for r in rows(db,'kv_item_captures') if r['item_id'] in direct
              and (include_personal or r['namespace'] in content)]
    for row in captures:
        value=json.loads(row['payload_json'])
        if row['namespace']=='portable.manifest':library_ids.add(value['library']['id'])
    selections={
      'kv_libraries':[r for r in rows(db,'kv_libraries') if r['id'] in library_ids],
      'kv_library_sources':[r for r in rows(db,'kv_library_sources') if r['library_id'] in library_ids
           and (r['namespace']!='files.source' or r['legacy_key']==source_id)],
      'kv_items':[r for r in rows(db,'kv_items') if r['id'] in selected],
      'kv_legacy_keys':[r for r in rows(db,'kv_legacy_keys') if r['item_id'] in direct],
      'kv_assets':[r for r in rows(db,'kv_assets') if r['id'] in assets],
      'kv_item_assets':links,
      'kv_item_relations':[r for r in relations if r['source_item_id'] in selected and r['target_item_id'] in selected],
      'kv_item_captures':captures}
    for table,values in selections.items():_write_rows(packet,db,'suite','catalog',table,values)
    return direct


@dataclass
class Selection:
    context: object
    files: dict
    hashes: set
    job_ids: set
    file_ids: set


def _selection(context,files,hashes):
    job_ids=set()
    if _table(context.connection,'kv_item_captures'):
        for capture in rows(context.connection,'kv_item_captures',"namespace='youtube.download'"):
            payload=json.loads(capture['payload_json'])
            if (payload['record']['source_id']==context.source.source_id
                    and selected_path(context,payload['record']['path'],files)):job_ids.update(payload['legacy_job_ids'])
    file_ids=set()
    path=Path(context.index_path)
    if path.is_file():
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            for row in rows(db,'files'):
                try:relative=Path(row['path']).relative_to(context.source.path).as_posix()
                except ValueError:continue
                if relative in files:file_ids.add(row['id'])
    return Selection(context,files,hashes,job_ids,file_ids)


def preview_export(contexts,include_personal=False):
    if type(include_personal) is not bool:raise ValueError('Invalid personal-record choice')
    count=size=0;roots=[]
    for context in contexts:
        selected=inventory(context)
        count+=len(selected);size+=sum(p.stat().st_size for p in selected.values())
        roots.append(dict(id=context.source.source_id,name=context.source.display_name,
                          owner=context.source.role,files=len(selected)))
    return dict(sources=roots,files=count,bytes=size,include_personal=include_personal)


@observed('suite', 'collection.export')
def export_collection(contexts,destination,*,request_id,include_personal=False,configuration=None):
    """Create a portable folder; selected roots keep every original relative name."""
    contexts=tuple(contexts)
    if not contexts or len(contexts)>64:raise ValueError('Select between one and 64 registered folders')
    portable._identity(request_id,'collection request')
    if not str(destination).strip():raise ValueError('Choose a new destination folder')
    destination=Path(destination).absolute()
    if destination.resolve()!=destination:raise ValueError('Export destination contains links')
    for context in contexts:
        context.resolve('')
        if destination.is_relative_to(Path(context.source.path).resolve()) or destination.is_relative_to(context.suite_home.resolve()):
            raise ValueError('Choose a new export folder outside source and app data')
    identity=dict(format=FORMAT,version=VERSION,request_id=request_id,
        sources=[dict(id=c.source.source_id,path=c.source.path,owner=c.source.role) for c in contexts],
        include_personal=include_personal,destination=str(destination))
    stage=destination.with_name('.'+destination.name+'.collection-'+request_id)
    stage.parent.mkdir(parents=True,exist_ok=True)
    portable_path(stage.parent,stage.name,(),must_exist=False);stage.mkdir(exist_ok=True)
    with portable._attempt_lock(stage),ExitStack() as resources:
        portable._create(stage/'request.json',portable.encode(identity))
        if destination.exists() and (destination/'collection.json').exists():
            manifest=inspect_collection(destination,verify_files=True)
            if manifest['request']!=identity:raise ValueError('Collection destination belongs to another request')
            return dict(status='exported',path=str(destination),files=manifest['files'],sources=len(contexts))
        claim=destination/'.keivotos/collection-export.json'
        if destination.exists() and (not claim.is_file() or
                json.loads(portable._metadata_bytes(claim))!=identity):
            raise ValueError('Existing destination has no matching collection receipt')
        database=stage/'collection.sqlite'
        current_contexts=contexts
        recovery=current_contexts[0].suite_home/'recovery'/('collection-export-'+request_id)
        portable_path(recovery.parent,recovery.name,(),must_exist=False);recovery.mkdir(parents=True,exist_ok=True)
        portable._create(recovery/'request.json',portable.encode(identity))
        if not (stage/'prepared.json').exists():
            from services.data_migrations import snapshot_user_database
            cursor=current_contexts[0].connection.cursor();cursor.row_factory=None
            try:source_database=Path(next(row[2] for row in cursor.execute('PRAGMA database_list') if row[1]=='main'))
            finally:cursor.close()
            snapshot=recovery/'essential.sqlite'
            if not snapshot.exists():snapshot_user_database(source_database,snapshot)
            snapshot_db=resources.enter_context(closing(sqlite3.connect(snapshot)));snapshot_db.row_factory=sqlite3.Row
            contexts=tuple(replace(c,connection=snapshot_db) for c in current_contexts)
            if database.exists():database.rename(stage/('interrupted-'+uuid4().hex+'.sqlite'))
            with closing(sqlite3.connect(database)) as packet:
                packet.executescript(PACKET_SCHEMA)
                for context in contexts:
                    source=context.source
                    packet.execute('INSERT INTO sources VALUES(?,?)',(source.source_id,_json(vars(source))))
                    files=inventory(context);hashes=set()
                    for relative,path in files.items():
                        info=portable.asset(relative,asset_id='copy')
                        observation=portable.observe(source.path,info,context.forbidden)
                        with path.open('rb') as stream:md5=hashlib.file_digest(stream,'md5').hexdigest()
                        if portable.observe(source.path,info,context.forbidden,hash_bytes=False)['identity']!=observation['identity']:
                            raise ValueError('Original changed during collection selection')
                        hashes.add(md5)
                        target=portable_path(stage,'roots/'+source.source_id+'/'+relative,(),must_exist=False)
                        target.parent.mkdir(parents=True,exist_ok=True);portable.copy_observation(observation,target)
                        packet.execute('INSERT INTO files VALUES(?,?,?,?,?)',
                            (source.source_id,relative,observation['size'],observation['sha256'],md5))
                    item_ids=_catalog_records(context,packet,include_personal) or set()
                    preserved=context.suite_home/'preserved'
                    if preserved.exists():
                        for library_dir in sorted(preserved.iterdir()):
                            portable._identity(library_dir.name,'preserved library')
                            for item_id in sorted(item_ids):
                                directory=library_dir/item_id
                                if not directory.exists():continue
                                for base,dirs,names in os.walk(directory,followlinks=False):
                                    for name in [*dirs,*names]:
                                        portable_path(preserved,(Path(base)/name).relative_to(preserved).as_posix())
                                    for name in names:
                                        if name.endswith('.partial') or name=='.prepare.lock':continue
                                        path=Path(base)/name
                                        relative='preserved/'+path.relative_to(preserved).as_posix()
                                        observation=portable.observe(preserved,portable.asset(path.relative_to(preserved).as_posix(),asset_id='preserved'))
                                        target=portable_path(stage,relative,(),must_exist=False);target.parent.mkdir(parents=True,exist_ok=True)
                                        portable.copy_observation(observation,target)
                                        packet.execute('INSERT OR IGNORE INTO metadata VALUES(?,?,?,?,?,?)',
                                            (source.role,source.source_id,relative,relative,observation['size'],observation['sha256']))
                    selection=_selection(context,files,hashes)
                    descriptor=context.registry.require(source.role)
                    owner=descriptor.transfer_provider() if descriptor.transfer_provider else None
                    if owner is not None and include_personal:
                        providers=[d.transfer_provider() for d in context.registry if d.transfer_provider]
                        seen=set()
                        for provider in providers:
                            if provider.slug in seen:continue
                            seen.add(provider.slug)
                            if provider.select_records is None:continue
                            for table,values in provider.select_records(selection):
                                _write_rows(packet,context.connection,provider.slug,'personal',table,values)
                    if owner is not None and include_personal:
                        from services import asset_preservation
                        if configuration is None:
                            import config as configuration
                        keys=set()
                        for entry in packet.execute("SELECT owner,table_name,columns_json,row_json FROM records WHERE kind='personal'"):
                            fields=json.loads(entry[2]);data=dict(zip(fields,_unpack(entry[3])))
                            if entry[0]=='files' and entry[1]=='files_annotation_attachments':
                                keys.add(('files','attachment:'+str(data['id'])))
                            if entry[0]=='danbooru' and entry[1]=='artist_profile_assets':
                                keys.add(('danbooru','profile:'+str(data['id'])))
                        if keys:
                            reference_root=stage/'references'/source.source_id
                            asset_preservation.snapshot(snapshot,context.suite_home,context.registry,configuration,
                                reference_root,include_owners={o for o,_ in keys},include_references=keys)
                            # Only bindings for selected personal files leave this profile.
                            bindings=[r for r in rows(snapshot_db,'kv_preserved_assets')
                                      if (r['owner'],r['reference_key']) in keys]
                            _write_rows(packet,snapshot_db,'suite','preservation','kv_preserved_assets',bindings)
                    if owner is not None and owner.source_records is not None:
                        with closing(sqlite3.connect(context.index_path)) as index_db:
                            for table,values in owner.source_records(selection,index_db):
                                _write_rows(packet,index_db,owner.slug,'index',table,values)
                    if owner is not None and owner.metadata_files is not None:
                        for subject,path in owner.metadata_files(selection,configuration):
                            info=portable.asset(Path(path).name,asset_id='facts')
                            observation=portable.observe(Path(path).parent,info)
                            relative='metadata/'+source.source_id+'/'+observation['sha256']+'.json'
                            target=portable_path(stage,relative,(),must_exist=False);target.parent.mkdir(parents=True,exist_ok=True)
                            portable.copy_observation(observation,target)
                            packet.execute('INSERT INTO metadata VALUES(?,?,?,?,?,?)',
                                (source.role,source.source_id,subject,relative,observation['size'],observation['sha256']))
                packet.commit()
                if packet.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Collection metadata failed verification')
            if database.stat().st_size>MAX_METADATA:raise ValueError('Collection metadata exceeds its limit')
            snapshot_db.close()
            raw_hash=hashlib.sha256(database.read_bytes()).hexdigest()
            with closing(sqlite3.connect(database)) as db:count=db.execute('SELECT count(*) FROM files').fetchone()[0]
            manifest=dict(format=FORMAT,version=VERSION,request=identity,database_sha256=raw_hash,files=count)
            portable._create(stage/'prepared.json',portable.encode(manifest))
        manifest=json.loads(portable._metadata_bytes(stage/'prepared.json'))
        for context in current_contexts:
            if (sources.get_source(context.connection,context.source.source_id)!=context.source
                    or {s.source_id:s for s in sources.list_sources(context.connection)}!={s.source_id:s for s in context.registered}):
                raise ValueError('Registered source changed during collection export')
        # Publish a complete tree exclusively. Incomplete owned output is retained.
        if not destination.exists():
            destination.mkdir()
            claim.parent.mkdir()
            portable._create(claim,portable.encode(identity))
        for directory,dirs,files in os.walk(stage):
            base=Path(directory)
            for filename in files:
                relative=(base/filename).relative_to(stage).as_posix()
                if relative in ('request.json','prepared.json','.prepare.lock') or relative.startswith('interrupted-'):continue
                target=portable_path(destination,relative,(),must_exist=False);target.parent.mkdir(parents=True,exist_ok=True)
                if target.exists():
                    info=portable.asset(filename,asset_id='retry')
                    if portable.observe(target.parent,info)['sha256']!=portable.observe(base,info)['sha256']:
                        raise ValueError('Interrupted collection contains conflicting bytes')
                else:os.link(base/filename,target)
        for directory,_,_ in os.walk(destination,topdown=False):portable._flush_directory(directory)
        portable._create(destination/'collection.json',portable.encode(manifest))
        inspect_collection(destination,verify_files=True)
        return dict(status='exported',path=str(destination),files=manifest['files'],sources=len(contexts))


def inspect_collection(directory,*,verify_files=False):
    directory=Path(directory).absolute()
    manifest=json.loads(portable._metadata_bytes(portable_path(directory,'collection.json')),object_pairs_hook=portable._unique)
    if (set(manifest)!={'format','version','request','database_sha256','files'}
            or manifest['format']!=FORMAT or type(manifest['version']) is not int or manifest['version']!=VERSION):
        raise ValueError('Unsupported collection format')
    request=manifest['request']
    if (not isinstance(request,dict) or set(request)!={'format','version','request_id','sources','include_personal','destination'}
            or request['format']!=FORMAT or request['version']!=VERSION or type(request['include_personal']) is not bool
            or type(manifest['files']) is not int or manifest['files']<0):
        raise ValueError('Invalid collection request')
    portable._identity(request['request_id'],'collection request')
    database=portable_path(directory,'collection.sqlite')
    if database.stat().st_size>MAX_METADATA or hashlib.sha256(database.read_bytes()).hexdigest()!=manifest['database_sha256']:
        raise ValueError('Collection metadata changed')
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row;db.execute('PRAGMA trusted_schema=OFF')
        if db.execute('PRAGMA user_version').fetchone()[0]!=1:raise ValueError('Unsupported collection metadata version')
        schema=[tuple(r) for r in db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")]
        if set(schema)!={('table',t) for t in ('sources','files','records','metadata')}:
            raise ValueError('Unknown collection metadata schema')
        roots=_root_rows(db)
        if not roots or len(roots)>64:raise ValueError('Invalid collection roots')
        for source_id,source in roots.items():
            portable._identity(source_id,'collection source')
            if source['source_id']!=source_id:raise ValueError('Collection root identity changed')
        if db.execute('SELECT count(*) FROM files').fetchone()[0]!=manifest['files']:
            raise ValueError('Collection file count changed')
        if verify_files:
            seen=set()
            for row in db.execute('SELECT * FROM files'):
                if row['source_id'] not in roots:raise ValueError('Unknown collection file root')
                relative='roots/'+row['source_id']+'/'+portable_relative(row['path'])
                import unicodedata
                key=unicodedata.normalize('NFC',relative).casefold()
                if key in seen:raise ValueError('Collection paths collide on another platform')
                seen.add(key)
                portable.observe(directory,portable.asset(relative,asset_id='verify',size=row['size'],sha256=row['sha256']))
            for row in db.execute('SELECT * FROM metadata'):
                if row['source_id'] not in roots:raise ValueError('Unknown collection metadata root')
                if not row['path'].startswith(('metadata/'+row['source_id']+'/','preserved/')):
                    raise ValueError('Collection metadata is outside its declared store')
                portable.observe(directory,portable.asset(row['path'],asset_id='metadata',size=row['size'],sha256=row['sha256']))
        from services.asset_preservation import validate_snapshot
        for source_id in roots:
            references=directory/'references'/source_id
            if references.exists():validate_snapshot(references)
    return manifest


def _packet_rows(db):
    return rows(db,'records')


def _allowed(configuration,owner,kind,table):
    if kind=='catalog':return owner=='suite' and table in CATALOG_TABLES
    if kind=='preservation':return owner=='suite' and table=='kv_preserved_assets'
    descriptor=configuration.MODULE_REGISTRY.get(owner)
    if descriptor is None:raise ValueError('The collection requires an unavailable module')
    if kind=='personal':
        contribution=descriptor.transfer_provider() if descriptor.transfer_provider else None
        return contribution is not None and table in contribution.personal_tables
    if kind=='index':
        return descriptor.catalog_index_provider is not None and table in descriptor.catalog_index_provider().tables
    return False


def _definition(db,table):
    cursor=db.cursor();cursor.row_factory=None
    try:definition=list(cursor.execute('PRAGMA table_info('+quoted(table)+')'))
    finally:cursor.close()
    return [r[1] for r in definition],[r[1] for r in sorted(definition,key=lambda r:r[5]) if r[5]]


def _rebase(value,roots,destination,*,table):
    """Translate known location fields; authored text and captured JSON stay exact."""
    result=dict(value)
    for field in ('file_path',*(['path'] if table=='files' else [])):
        old=result.get(field)
        if not isinstance(old,str) or not old:continue
        normalized=old.replace('\\','/')
        for source_id,source in roots.items():
            prefix=source['path'].replace('\\','/').rstrip('/')
            from pathlib import PureWindowsPath
            if PureWindowsPath(source['path']).drive:
                try:relative=PureWindowsPath(old).relative_to(PureWindowsPath(source['path'])).as_posix()
                except ValueError:continue
                if relative=='.':relative=''
            elif normalized==prefix or normalized.startswith(prefix+'/'):
                relative=normalized[len(prefix):].lstrip('/')
            else:continue
            if relative:portable_relative(relative)
            result[field]=str(Path(destination)/'roots'/source_id/relative)
            break
    return result


def _merge_row(db,table,columns,keys,value):
    current_columns,current_keys=_definition(db,table)
    if (not keys or keys!=current_keys or len(columns)!=len(set(columns))
            or not set(columns)<=set(current_columns) or set(value)!=set(columns)):
        raise ValueError('The collection record schema is unsupported: '+table)
    if table=='kv_item_captures':
        payload=json.loads(value['payload_json'],object_pairs_hook=portable._unique)
        encoded,digest=item_catalog._capture_payload(payload)
        if value['payload_json']!=encoded or value['payload_sha256']!=digest:
            raise ValueError('Captured collection metadata changed')
    if table=='kv_item_relations':
        found=db.execute("""WITH RECURSIVE reachable(id) AS (
          SELECT ? UNION SELECT r.target_item_id FROM kv_item_relations r JOIN reachable n ON r.source_item_id=n.id)
          SELECT 1 FROM reachable WHERE id=?""",(value['target_item_id'],value['source_item_id'])).fetchone()
        if found:raise ValueError('Cyclic collection relationships')
    cursor=db.cursor();cursor.row_factory=sqlite3.Row
    try:
        existing=cursor.execute('SELECT '+','.join(quoted(c) for c in columns)+' FROM '+quoted(table)+
          ' WHERE '+' AND '.join(quoted(k)+' IS ?' for k in keys),tuple(value[k] for k in keys)).fetchone()
        if existing is not None:
            if dict(existing)!=value:raise ValueError('Collection conflicts with an existing record: '+table)
            return
        cursor.execute('INSERT INTO '+quoted(table)+'('+','.join(quoted(c) for c in columns)+') VALUES('+
                       ','.join('?' for _ in columns)+')',tuple(value[c] for c in columns))
    finally:cursor.close()


def _root_rows(packet):
    result={}
    for row in rows(packet,'sources'):
        value=json.loads(row['payload'],object_pairs_hook=portable._unique)
        if (set(value)!={'source_id','path','display_name','role','visible','added_at'}
                or not isinstance(value['display_name'],str) or not value['display_name']
                or not isinstance(value['path'],str) or type(value['visible']) is not bool):
            raise ValueError('Invalid collection folder declaration')
        if value['source_id']!=row['source_id']:raise ValueError('Collection source identity changed')
        portable._identity(row['source_id'],'collection source')
        result[row['source_id']]=value
    return result


def _prepare_records(essential,derived,packet,configuration,destination,include_personal):
    roots=_root_rows(packet)
    if not roots:raise ValueError('The collection has no folders')
    records=_packet_rows(packet)
    for source in roots.values():
        if configuration.MODULE_REGISTRY.get(source['role']) is None:raise ValueError('Collection module is unavailable')
    item_catalog.ensure_catalog_schema(essential)
    sources.ensure_sources_schema(essential)
    from services.asset_preservation import SCHEMA as ASSET_SCHEMA
    essential.executescript(ASSET_SCHEMA+';'+IMPORT_SCHEMA)
    for row in records:
        owner,kind,table=row['owner'],row['kind'],row['table_name']
        if not _allowed(configuration,owner,kind,table):raise ValueError('Unknown collection record ownership')
        if kind=='personal' and not include_personal:continue
        if kind=='personal':
            descriptor=configuration.MODULE_REGISTRY.require(owner)
            if owner=='files':
                from files_base.annotations import ensure_annotations_schema
                ensure_annotations_schema(essential)
            elif descriptor.user_schema_provider is not None:essential.executescript(descriptor.user_schema_provider())
        if kind=='index':
            from services.catalog_index import ensure_owner,is_combined
            owner_declaration=configuration.MODULE_REGISTRY.require(owner).catalog_index_provider()
            if is_combined(derived):ensure_owner(derived,owner_declaration)
            else:owner_declaration.initialize(derived)
    essential.commit();derived.commit()
    essential.execute('BEGIN IMMEDIATE');derived.execute('BEGIN IMMEDIATE')
    essential.execute('PRAGMA defer_foreign_keys=ON');derived.execute('PRAGMA defer_foreign_keys=ON')
    try:
        for source_id,source in roots.items():
            location=str(Path(destination)/'roots'/source_id)
            existing=sources.get_source(essential,source_id)
            if existing is not None:
                if existing.path!=location or existing.role!=source['role']:
                    raise ValueError('This folder identity is already registered at another location')
            else:
                essential.execute('INSERT INTO files_sources(source_id,path,display_name,role,visible,added_at) VALUES(?,?,?,?,?,?)',
                    (source_id,location,source['display_name'],source['role'],int(source['visible']),source['added_at']))
        for source_id,source in roots.items():
            contribution=configuration.MODULE_REGISTRY.require(source['role']).transfer_provider()
            if contribution.import_source is not None:
                contribution.import_source(essential,source,Path(destination)/'roots'/source_id)
        for row in records:
            if row['kind'] in ('personal','preservation') and not include_personal:continue
            columns=json.loads(row['columns_json']);keys=json.loads(row['keys_json'])
            unpacked=_unpack(row['row_json'])
            if len(columns)!=len(unpacked):raise ValueError('Collection record values changed')
            original=dict(zip(columns,unpacked))
            value=_rebase(original,roots,destination,table=row['table_name'])
            if row['kind']=='index' and row['table_name']=='files':
                if value.get('path')==original.get('path'):raise ValueError('Collection media is outside selected folders')
                relative=Path(value['path']).relative_to(destination).as_posix().split('/',2)
                if len(relative)!=3 or not packet.execute('SELECT 1 FROM files WHERE source_id=? AND path=?',tuple(relative[1:])).fetchone():
                    raise ValueError('Collection metadata references unlisted media')
            db=derived if row['kind']=='index' else essential
            _merge_row(db,row['table_name'],columns,keys,value)
        if essential.execute('PRAGMA foreign_key_check').fetchone() or derived.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Collection record relationships are incomplete')
        essential.commit();derived.commit()
    except sqlite3.IntegrityError as exc:
        essential.rollback();derived.rollback()
        raise ValueError('Collection records conflict or have incomplete relationships') from exc
    except BaseException:
        essential.rollback();derived.rollback();raise
    return roots


def _copy_database_to_memory(path):
    target=sqlite3.connect(':memory:');target.row_factory=sqlite3.Row
    with closing(sqlite3.connect(Path(path).resolve(strict=True).as_uri()+'?mode=ro',uri=True)) as source:
        source.backup(target)
    return target


def _destination(configuration,destination):
    destination=Path(destination).absolute()
    if destination.resolve()!=destination or destination.is_relative_to(Path(configuration.SUITE_HOME).resolve()):
        raise ValueError('Choose a new collection folder outside app data')
    portable_path(destination.parent,destination.name,(),must_exist=False)
    return destination


def preview_import(directory,destination,configuration,*,include_personal=False):
    manifest=inspect_collection(directory,verify_files=True)
    if include_personal and not manifest['request']['include_personal']:
        raise ValueError('This collection contains no personal records')
    destination=_destination(configuration,destination)
    if destination.exists():raise ValueError('Choose a new folder for the imported collection')
    database=Path(directory)/'collection.sqlite'
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)) as packet, \
         closing(_copy_database_to_memory(configuration.USER_DB_PATH)) as essential, \
         closing(_copy_database_to_memory(configuration.INDEX_DB_PATH)) as derived:
        packet.row_factory=sqlite3.Row
        roots=_prepare_records(essential,derived,packet,configuration,destination,include_personal)
        personal=packet.execute("SELECT count(*) FROM records WHERE kind='personal'").fetchone()[0]
    return dict(sources=[dict(id=k,name=v['display_name'],owner=v['role']) for k,v in roots.items()],
                files=manifest['files'],personal_records=personal if include_personal else 0,
                include_personal=include_personal,destination=str(destination))


@observed('suite', 'collection.import')
def import_collection(directory,destination,configuration,*,request_id,include_personal=False):
    """Verify and stage everything, then journal the essential/index acknowledgement."""
    from services import storage_authority as authority,asset_preservation
    from services.data_migrations import snapshot_user_database
    from database_connections import exclusive_database_access
    from maintenance import exclusive_tool_operation
    portable._identity(request_id,'collection request')
    directory=Path(directory).absolute();destination=_destination(configuration,destination)
    manifest=inspect_collection(directory,verify_files=True)
    packet_hash=manifest['database_sha256']
    with closing(sqlite3.connect(configuration.USER_DB_PATH)) as db:
        if _table(db,'kv_collection_imports'):
            existing=rows(db,'kv_collection_imports','request_id=?',(request_id,))
            if existing:
                if existing[0]['packet_sha256']!=packet_hash or existing[0]['destination']!=str(destination) or bool(existing[0]['include_personal'])!=include_personal:
                    raise ValueError('Import request already belongs to another collection')
                return dict(status='imported',path=str(destination),sources=len(json.loads(existing[0]['sources_json'])),
                            files=manifest['files'],restart_required=False)
    attempt=Path(configuration.SUITE_HOME)/'recovery'/('collection-import-'+request_id)
    attempt.mkdir(parents=True,exist_ok=True)
    selection=dict(packet=str(directory),sha256=packet_hash,destination=str(destination),include_personal=include_personal)
    portable._create(attempt/'request.json',portable.encode(selection))
    claim=destination/'.keivotos/collection-import.json'
    if destination.exists():
        if not claim.is_file() or json.loads(portable._metadata_bytes(claim))!=selection:
            raise ValueError('Import folder belongs to another operation')
    else:
        preview_import(directory,destination,configuration,include_personal=include_personal)
        destination.mkdir(parents=True)
        claim.parent.mkdir();portable._create(claim,portable.encode(selection))
    frozen=attempt/'collection.sqlite'
    info=portable.asset('collection.sqlite',asset_id='collection-records',sha256=packet_hash)
    portable.copy_observation(portable.observe(directory,info),frozen)
    with closing(sqlite3.connect(frozen.resolve().as_uri()+'?mode=ro',uri=True)) as packet:
        packet.row_factory=sqlite3.Row
        roots=_root_rows(packet)
        for row in packet.execute('SELECT * FROM files'):
            relative='roots/'+row['source_id']+'/'+portable_relative(row['path'])
            info=portable.asset(relative,asset_id='import',size=row['size'],sha256=row['sha256'])
            observation=portable.observe(directory,info)
            target=portable_path(destination,relative,(),must_exist=False);target.parent.mkdir(parents=True,exist_ok=True)
            portable.copy_observation(observation,target)
        for entry in packet.execute('SELECT * FROM metadata'):
            if entry['path'].startswith('preserved/'):
                observation=portable.observe(directory,portable.asset(entry['path'],asset_id='preserved',
                    size=entry['size'],sha256=entry['sha256']))
                target=portable_path(configuration.SUITE_HOME,entry['path'],(),must_exist=False)
                target.parent.mkdir(parents=True,exist_ok=True);portable.copy_observation(observation,target)
        if include_personal:
            for source_id in roots:
                references=directory/'references'/source_id
                if references.exists():asset_preservation.restore_snapshot(references,configuration.SUITE_HOME)
        with exclusive_tool_operation('importing a collection',wait=False),exclusive_database_access():
            essential=attempt/('essential-'+uuid4().hex+'.sqlite')
            derived=attempt/('index-'+uuid4().hex+'.sqlite')
            snapshot_user_database(configuration.USER_DB_PATH,essential)
            snapshot_user_database(configuration.INDEX_DB_PATH,derived)
            with authority.connection(essential) as user,authority.connection(derived) as index_db:
                _prepare_records(user,index_db,packet,configuration,destination,include_personal)
                with user:
                    user.execute('INSERT INTO kv_collection_imports VALUES(?,?,?,?,?)',
                        (request_id,packet_hash,str(destination),_json(sorted(roots)),int(include_personal)))
                for metadata in packet.execute("SELECT * FROM metadata"):
                    if metadata['path'].startswith('preserved/'):continue
                    source=roots.get(metadata['source_id'])
                    if source is None or source['role']!=metadata['owner']:
                        raise ValueError('Metadata ownership differs from its selected folder')
                    relative=portable_relative(metadata['subject'])
                    if not packet.execute('SELECT 1 FROM files WHERE source_id=? AND path=?',
                                          (metadata['source_id'],relative)).fetchone():
                        raise ValueError('Source metadata references unlisted collection media')
                    metadata_path=portable_path(directory,metadata['path'])
                    if metadata_path.stat().st_size>16*1024*1024:raise ValueError('Source metadata exceeds its limit')
                    raw=metadata_path.read_bytes()
                    root=destination/'roots'/metadata['source_id']
                    descriptor=configuration.MODULE_REGISTRY.require(metadata['owner'])
                    contribution=descriptor.transfer_provider()
                    if descriptor.catalog_index_provider:
                        from services.catalog_index import ensure_owner
                        ensure_owner(index_db,descriptor.catalog_index_provider())
                    if contribution.import_metadata is None:raise ValueError('Metadata restore owner is unavailable')
                    contribution.import_metadata(index_db,user,root,relative,raw,metadata['sha256'])
                for source_id in roots:
                    root=destination/'roots'/source_id
                    index.scan_source(index_db,source_id,root,excluded_roots=[configuration.SUITE_HOME],commit=False)
                index_db.commit()
                if any(s['role']=='danbooru' for s in roots.values()):
                    from services.catalog_index import preserve_owner
                    preserve_owner(user,index_db,configuration.MODULE_REGISTRY.require('danbooru').catalog_index_provider())
            # Current paths are unchanged until both candidates validate.
            authority.install_databases(configuration.SUITE_HOME,attempt,
                [('essential',essential,configuration.USER_DB_PATH),('index',derived,configuration.INDEX_DB_PATH)])
    return dict(status='imported',path=str(destination),sources=len(roots),files=manifest['files'],restart_required=False)


def admitted_path(context,relative):
    if not isinstance(relative,str):return False
    try:context.resolve(relative,must_exist=False)
    except (ValueError,OSError,serving.ServeDenied):return False
    return True


def selected_path(context,relative,files):
    if not admitted_path(context,relative):return False
    if relative in files:return True
    prefix=relative.rstrip('/')+'/' if relative else ''
    return any(name.startswith(prefix) for name in files)

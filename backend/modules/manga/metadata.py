"""Manga-owned saved metadata, explicit source refresh and personal field choices."""
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
import hashlib
import json
import sqlite3
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from fastapi import HTTPException
from files_base import sources, serving
from services import item_catalog, item_manifest as portable, local_metadata as formats

SCHEMA="""
CREATE TABLE IF NOT EXISTS manga_metadata_overrides(
 source_id TEXT NOT NULL,path TEXT NOT NULL,kind TEXT NOT NULL,
 version INTEGER NOT NULL CHECK(version>0),fields_json TEXT NOT NULL,
 updated_at TEXT NOT NULL DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 PRIMARY KEY(source_id,path,kind));
CREATE TABLE IF NOT EXISTS manga_metadata_updates(
 request_id TEXT PRIMARY KEY,source_id TEXT NOT NULL,path TEXT NOT NULL,kind TEXT NOT NULL,
 request_json TEXT NOT NULL,response_json TEXT NOT NULL);
"""
SOURCE='manga.metadata.source'
HEAD='manga.metadata.head'
PERSONAL='manga.metadata.personal'
KINDS=('series',)


def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':'))
def digest(value):return hashlib.sha256(encoded(value).encode()).hexdigest()
def key(source_id,path,kind):return (source_id,path,kind)
def exists(db,table):return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone() is not None

def supplied(value,*,personal=False):
    if not isinstance(value,dict):raise formats.MetadataError('Invalid detail fields.')
    return formats.fields({k:v for k,v in value.items() if k in formats.FIELDS},personal=personal)



def context(db,source_id,*,index_path=None,suite_home=None,registry=None):
    import config
    registered=tuple(sources.list_sources(db))
    source=next((s for s in registered if s.source_id==source_id and s.visible and s.role=='manga'),None)
    if source is None:raise HTTPException(404,'Manga is unavailable.')
    return portable.Context(source,registered,Path(suite_home or config.SUITE_HOME),Path(index_path or config.FILES_DB_PATH),db,registry or config.MODULE_REGISTRY)


def manga_rows(ctx,path=None,kind=None):
    from modules.manga import library
    result=[]
    for series in library.catalog(ctx.index_path,[ctx.source]):
        if path is not None and series['path']!=path:continue
        chapters=[]
        for chapter in series['chapters']:
            try:
                target=ctx.resolve(chapter['path'])
                if chapter['kind']=='folder':
                    admitted=False
                    for page in library.folder_pages(ctx.index_path,ctx.source.source_id,chapter['path']):
                        try:
                            if ctx.resolve(page).is_file():admitted=True;break
                        except (OSError,ValueError,serving.ServeDenied):continue
                    if not admitted:continue
                elif not target.is_file():continue
                chapters.append(chapter)
            except (OSError,ValueError,serving.ServeDenied):continue
        if chapters:result.append(dict(path=series['path'],chapters=chapters))
    return result


def validate_subject(ctx,path,kind='series'):
    if kind!='series':raise HTTPException(422,'Unknown detail scope.')
    try:
        from storage_layout import portable_relative
        if path:portable_relative(path)
        if path and '/' in path:raise ValueError('Series scope')
        target=ctx.resolve(path)
        if not target.is_dir() or not manga_rows(ctx,path,kind):raise ValueError('Unavailable')
    except (OSError,ValueError,serving.ServeDenied) as error:raise HTTPException(404,'Manga details are unavailable.') from error
    return target


def scopes(path,kind):return [(path,'series')]


def _subject_id(ctx,path,kind):
    if not exists(ctx.connection,'kv_catalog_meta'):item_catalog.ensure_catalog_schema(ctx.connection)
    identity=str(uuid5(NAMESPACE_URL,'urn:keivotos:manga:metadata:'+encoded([ctx.source.source_id,path,kind])))
    item_catalog.ensure_item(ctx.connection,namespace='manga.metadata.subject',legacy_key=encoded([ctx.source.source_id,path,kind]),owner='manga',item_type='metadata',item_id=identity)
    return identity


def saved(db,source_ids=None):
    result={}
    if not exists(db,'kv_item_captures'):return result
    cursor=db.cursor();cursor.row_factory=sqlite3.Row
    try:
        for row in cursor.execute("SELECT c.item_id,c.capture_key FROM kv_item_captures c JOIN kv_items i ON i.id=c.item_id WHERE c.namespace=? AND i.owner='manga' AND c.capture_key=(SELECT CAST(MAX(CAST(h.capture_key AS INTEGER)) AS TEXT) FROM kv_item_captures h WHERE h.item_id=c.item_id AND h.namespace=c.namespace) ORDER BY c.item_id",(HEAD,)):
            try:
                head=item_catalog.read_capture(db,item_id=row['item_id'],namespace=HEAD,capture_key=row['capture_key'])['payload']
                if source_ids is not None and head['source_id'] not in source_ids:continue
                capture=item_catalog.read_capture(db,item_id=row['item_id'],namespace=SOURCE,capture_key=head['revision'])
                if capture is None:continue
                value=capture['payload'];facts=supplied(value['facts'])
                if (head['source_id'],head['path'],head['kind'])!=(value['source_id'],value['path'],value['kind']) or head['kind'] not in KINDS:continue
                result[key(head['source_id'],head['path'],head['kind'])]=dict(facts=facts,version=head['version'],revision=head['revision'],inputs=value['inputs'],recorded_at=capture['recorded_at'])
            except (KeyError,TypeError,ValueError):continue
    finally:cursor.close()
    return result


def head(db,source_id,path,kind):
    if not exists(db,'kv_item_captures'):return None
    identity=str(uuid5(NAMESPACE_URL,'urn:keivotos:manga:metadata:'+encoded([source_id,path,kind])))
    row=db.execute('SELECT capture_key FROM kv_item_captures WHERE item_id=? AND namespace=? ORDER BY CAST(capture_key AS INTEGER) DESC LIMIT 1',(identity,HEAD)).fetchone()
    if not row:return None
    value=item_catalog.read_capture(db,item_id=identity,namespace=HEAD,capture_key=row['capture_key'])['payload']
    capture=item_catalog.read_capture(db,item_id=identity,namespace=SOURCE,capture_key=value['revision'])
    if not capture:raise formats.MetadataError('Saved details need review.')
    payload=capture['payload']
    return dict(facts=supplied(payload['facts']),version=value['version'],revision=value['revision'],inputs=payload['inputs'],recorded_at=capture['recorded_at'])


def personal(db,source_ids=None):
    if not exists(db,'manga_metadata_overrides'):return {}
    cursor=db.cursor();cursor.row_factory=sqlite3.Row
    try:
        result={}
        for row in cursor.execute('SELECT * FROM manga_metadata_overrides'):
            if source_ids is not None and row['source_id'] not in source_ids:continue
            try:choices=supplied(json.loads(row['fields_json']),personal=True)
            except (ValueError,TypeError):continue
            result[key(row['source_id'],row['path'],row['kind'])]=dict(fields=choices,version=row['version'])
        return result
    finally:cursor.close()


def effective(source_id,path,kind,source_records,personal_records):
    local={};lineage=[];chain=scopes(path,kind)
    for subject,scope in chain:
        record=source_records.get(key(source_id,subject,scope))
        if record:
            local.update(record['facts'])
            lineage.append([subject,scope,record['revision']])
    # A personal field choice takes priority; specific scopes can replace inherited choices.
    base=dict(local)
    def apply(target,choices):
        for field,value in choices.items():
            if value is None:target.pop(field,None)
            else:target[field]=value
    for subject,scope in chain[:-1]:
        record=personal_records.get(key(source_id,subject,scope))
        if record:
            apply(base,record['fields'])
            lineage.append([subject,scope,record['version']])
    record=personal_records.get(key(source_id,path,kind),dict(fields={},version=0));fields=dict(base);apply(fields,record['fields'])
    return dict(fields=fields,local_fields=base,overrides=record['fields'],version=record['version'],source_revision=digest(lineage))


def details(ctx,path,kind):
    validate_subject(ctx,path,kind)
    return dict(subject=dict(source_id=ctx.source.source_id,path=path,kind=kind),**effective(ctx.source.source_id,path,kind,saved(ctx.connection,{ctx.source.source_id}),personal(ctx.connection,{ctx.source.source_id})))


def native_entries(value):
    extension=value['extensions'].get('manga',{})
    if not isinstance(extension,dict):raise formats.MetadataError('Invalid Manga manifest details.')
    envelope=extension.get('metadata')
    if envelope is None:return supplied(extension)
    if not isinstance(envelope,dict) or set(envelope)!={'version','fields'} or type(envelope['version']) is not int or envelope['version']!=1:raise formats.MetadataError('Unsupported Manga metadata envelope.')
    return formats.fields(envelope['fields'])


def native_subjects(value,ctx,relative):
    from modules.manga import library
    folder=PurePosixPath(relative).parent.parent
    result=set()
    for asset in value['assets']:
        if asset['role'] not in ('archive','page'):continue
        full=(folder/asset['path']).as_posix()
        target=ctx.resolve(full)
        if not target.is_file() or target.suffix.lower().lstrip('.') not in library.IMAGES|library.ARCHIVES:raise formats.MetadataError('Manga details are not attached to readable pages.')
        parts=PurePosixPath(full).parts;result.add(parts[0] if len(parts)>1 else '')
    if not result:raise formats.MetadataError('Manga metadata has no chapter attachment.')
    return result


def read_native(ctx,path,kind):
    folders=[PurePosixPath(path),PurePosixPath('')]
    for row in manga_rows(ctx,path,kind):
        for chapter in row['chapters']:
            folder=PurePosixPath(chapter['path'])
            if chapter['kind']=='archive':folder=folder.parent
            folders.append(folder)
    seen=set()
    for folder in folders:
        relative=(folder/portable.MANIFEST).as_posix()
        if relative in seen:continue
        seen.add(relative);target=ctx.resolve(relative,must_exist=False)
        if not target.is_file():continue
        value,_=portable.read(ctx.source.path,relative,forbidden=ctx.forbidden,check_assets=False)
        if value['item']['owner']!='manga' or path not in native_subjects(value,ctx,relative):continue
        facts=native_entries(value)
        if not facts:continue
        raw=portable._metadata_bytes(target)
        return facts,[dict(path=relative,format='native',sha256=hashlib.sha256(raw).hexdigest(),identity=portable.file_identity(target.stat()))],[raw]
    return {},[],[]


def read_local(ctx,path,kind='series'):
    from modules.manga import library
    validate_subject(ctx,path,kind)
    def read_file(relative,format_name):
        target=ctx.resolve(relative,must_exist=False)
        if not target.is_file():return None
        raw=portable._metadata_bytes(target);facts=(formats.comic_info if format_name=='comic-info' else formats.details_json)(raw)
        return facts,[dict(path=relative,format=format_name,sha256=hashlib.sha256(raw).hexdigest(),identity=portable.file_identity(target.stat()))],[raw]
    for name,format_name in [('ComicInfo.xml','comic-info'),('details.json','details-json')]:
        found=read_file((PurePosixPath(path)/name).as_posix(),format_name)
        if found is not None:return found
    for row in manga_rows(ctx,path,kind):
        for chapter in row['chapters']:
            if chapter['kind']=='folder':
                found=read_file((PurePosixPath(chapter['path'])/'ComicInfo.xml').as_posix(),'comic-info')
                if found is not None:return found
            else:
                target=ctx.resolve(chapter['path'])
                with library.open_archive(target,source_id=ctx.source.source_id) as (archive,_):
                    try:entry=archive.getinfo('ComicInfo.xml')
                    except KeyError:continue
                    if entry.is_dir() or entry.file_size>formats.MAX_BYTES:raise formats.MetadataError('Embedded ComicInfo exceeds its size limit.')
                    with archive.open(entry) as stream:raw=stream.read(formats.MAX_BYTES+1)
                    facts=formats.comic_info(raw)
                return facts,[dict(path=chapter['path'],entry='ComicInfo.xml',format='comic-info',sha256=hashlib.sha256(raw).hexdigest(),identity=portable.file_identity(target.stat()))],[raw]
    return read_native(ctx,path,kind)


def _fresh(ctx,path,kind,inputs=None):
    current=context(ctx.connection,ctx.source.source_id,index_path=ctx.index_path,suite_home=ctx.suite_home,registry=ctx.registry)
    if current.source!=ctx.source:raise HTTPException(409,'Manga folder changed. Refresh details.')
    validate_subject(current,path,kind)
    if inputs is not None:
        _,observed,_=read_local(current,path,kind)
        if observed!=inputs:raise HTTPException(409,'Local details changed. Refresh again.')


def refresh_one(ctx,path,kind='series'):
    from storage_layout import preserved_item_path,portable_path
    facts,inputs,raws=read_local(ctx,path,kind)
    evidence=[{k:v for k,v in value.items() if k!='identity'} for value in inputs]
    payload=dict(source_id=ctx.source.source_id,path=path,kind=kind,facts=facts,inputs=evidence)
    revision=digest(payload);old=head(ctx.connection,ctx.source.source_id,path,kind)
    if old and old['revision']==revision:_fresh(ctx,path,kind,inputs);return False
    if not old and not facts and not inputs:return False
    if not exists(ctx.connection,'kv_catalog_meta'):item_catalog.ensure_catalog_schema(ctx.connection)
    ctx.connection.execute('BEGIN IMMEDIATE')
    try:
        identity=_subject_id(ctx,path,kind);library=portable.collection_identity(ctx.connection,ctx.source)
        directory=preserved_item_path(ctx.suite_home,library,identity);assets=[]
        for position,(value,raw) in enumerate(zip(inputs,raws)):
            name='ComicInfo.xml' if value['format']=='comic-info' else 'details.json' if value['format']=='details-json' else 'item.json'
            relative='originals/'+value['sha256']+'/'+name
            target=portable_path(directory,relative,(),must_exist=False);target.parent.mkdir(parents=True,exist_ok=True);portable._create(target,raw)
            assets.append(portable.asset(relative,role='metadata',position=position,asset_id=str(uuid5(NAMESPACE_URL,revision+':'+str(position))),size=len(raw),sha256=value['sha256']))
        if assets:
            value=portable.new(item_id=identity,owner='manga',item_type='metadata',library_id=library,storage='linked',assets=assets,extensions={'manga':dict(metadata=dict(version=1,fields=facts))})
            portable.preserve(ctx.suite_home,value,refresh=True);portable.preserve_capture(ctx.suite_home,value,directory,[])
        _fresh(ctx,path,kind,inputs)
        item_catalog.record_capture(ctx.connection,item_id=identity,namespace=SOURCE,capture_key=revision,format_version=1,payload=payload)
        item_catalog.record_capture(ctx.connection,item_id=identity,namespace=HEAD,capture_key=str((old or {}).get('version',0)+1),format_version=1,payload=dict(source_id=ctx.source.source_id,path=path,kind=kind,revision=revision,version=(old or {}).get('version',0)+1))
        _fresh(ctx,path,kind,inputs);ctx.connection.commit()
    except BaseException:ctx.connection.rollback();raise
    return True


def refresh(ctx,path=None,kind='series'):
    selected=[(row['path'],'series') for row in manga_rows(ctx,path,kind)]
    if path is not None:validate_subject(ctx,path,kind)
    updated=sum(int(refresh_one(ctx,relative,scope)) for relative,scope in selected)
    return dict(refreshed=updated,subjects=len(selected))


def after_rescan(source_id):
    from database import get_user_db
    from suite_modules import require_enabled
    import lifecycle
    with lifecycle.module_change_lock:
        require_enabled('manga')
        with get_user_db() as db:return dict(manga_metadata=refresh(context(db,source_id)))


def save_choices(ctx,path,kind,choices,version,source_revision,request_id):
    choices=formats.fields(choices,personal=True);validate_subject(ctx,path,kind)
    identity=_subject_id(ctx,path,kind)
    request=encoded(dict(source_id=ctx.source.source_id,path=path,kind=kind,fields=choices,version=version,source_revision=source_revision))
    ctx.connection.execute('BEGIN IMMEDIATE')
    try:
        prior=ctx.connection.execute('SELECT request_json,response_json FROM manga_metadata_updates WHERE request_id=?',(request_id,)).fetchone()
        if prior:
            if prior['request_json']!=request:raise HTTPException(409,'This save request already has different details.')
            _fresh(ctx,path,kind,None);ctx.connection.commit();return json.loads(prior['response_json'])
        current=details(ctx,path,kind)
        if current['version']!=version or current['source_revision']!=source_revision:raise HTTPException(409,'Details changed. Reload before saving.')
        if version>=9007199254740990:raise HTTPException(409,'Detail version needs review.')
        retained=ctx.connection.execute('SELECT fields_json FROM manga_metadata_overrides WHERE source_id=? AND path=? AND kind=?',(ctx.source.source_id,path,kind)).fetchone()
        hidden={k:v for k,v in json.loads(retained['fields_json']).items() if k not in formats.FIELDS} if retained else {}
        stored={**hidden,**choices}
        ctx.connection.execute("""INSERT INTO manga_metadata_overrides(source_id,path,kind,version,fields_json) VALUES(?,?,?,?,?)
            ON CONFLICT(source_id,path,kind) DO UPDATE SET version=excluded.version,fields_json=excluded.fields_json,
            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",(ctx.source.source_id,path,kind,version+1,encoded(stored)))
        response=details(ctx,path,kind)
        ctx.connection.execute('INSERT INTO manga_metadata_updates VALUES(?,?,?,?,?,?)',(request_id,ctx.source.source_id,path,kind,request,encoded(response)))
        item_catalog.record_capture(ctx.connection,item_id=identity,namespace=PERSONAL,capture_key='save:'+request_id,format_version=1,
            payload=dict(source_id=ctx.source.source_id,path=path,kind=kind,version=version+1,fields=choices))
        _fresh(ctx,path,kind,None);ctx.connection.commit();return response
    except BaseException:ctx.connection.rollback();raise


def import_catalog_records(db):
    """Independent metadata importer; originals and prior importer bodies stay intact."""
    if not db.in_transaction:raise ValueError('Import Manga details inside a transaction')
    point='manga_metadata_'+uuid4().hex;db.execute('SAVEPOINT '+point)
    cursor=db.cursor();cursor.row_factory=sqlite3.Row;count=0
    try:
        for table in ('manga_metadata_overrides','manga_metadata_updates'):
            if not exists(db,table):continue
            for row in cursor.execute('SELECT * FROM '+table):
                data=dict(row)
                if (not isinstance(data['source_id'],str) or not 0<len(data['source_id'])<=256 or '\0' in data['source_id'] or data['kind'] not in KINDS):raise ValueError('Invalid Manga metadata identity')
                from storage_layout import portable_relative
                if data['path']:portable_relative(data['path'])
                if data['path'] and '/' in data['path']:raise ValueError('Invalid Manga series path')
                if table.endswith('overrides'):
                    if type(data['version']) is not int or not 1<=data['version']<=9007199254740991:raise ValueError('Invalid metadata version')
                    supplied(json.loads(data['fields_json']),personal=True)
                else:
                    UUID(data['request_id'])
                    request=json.loads(data['request_json']);response=json.loads(data['response_json'])
                    if any(request[k]!=data[k] for k in ('source_id','path','kind')) or response['subject']!={k:data[k] for k in ('source_id','path','kind')}:raise ValueError('Invalid saved detail receipt')
                identity=str(uuid5(NAMESPACE_URL,'urn:keivotos:manga:metadata:'+encoded([data['source_id'],data['path'],data['kind']])))
                item_catalog.ensure_item(db,namespace='manga.metadata.subject',legacy_key=encoded([data['source_id'],data['path'],data['kind']]),owner='manga',item_type='metadata',item_id=identity)
                # SQL remains the personal authority. Anchors do not duplicate arbitrarily large retry responses.
                item_catalog.record_capture(db,item_id=identity,namespace=PERSONAL,capture_key='import:'+digest(data),format_version=1,
                    payload=dict(source_id=data['source_id'],path=data['path'],kind=data['kind'],table=table,record_hash=digest(data)))
                count+=1
        db.execute('RELEASE '+point);return count
    except BaseException:
        db.execute('ROLLBACK TO '+point);db.execute('RELEASE '+point);raise
    finally:cursor.close()


def snapshot(db,source_ids):return dict(source=saved(db,set(source_ids)),personal=personal(db,set(source_ids)))

def presentation(state,source_id,path,kind):
    return effective(source_id,path,kind,state['source'],state['personal'])['fields']

def matches(state,source_id,path,kind,term,*,filename=False):
    term=term.strip().casefold()
    if not term or term in (PurePosixPath(path).name if filename else path).casefold():return True
    facts=presentation(state,source_id,path,kind);values=[]
    for value in facts.values():values.extend(value if isinstance(value,list) else [value])
    return any(term in str(value).casefold() for value in values)


def import_manifest(value,ctx,relative):
    facts=native_entries(value)
    if not facts:return
    raw=portable._metadata_bytes(ctx.resolve(relative));native=dict(path=relative,format='native',sha256=hashlib.sha256(raw).hexdigest())
    for path in sorted(native_subjects(value,ctx,relative)):
        validate_subject(ctx,path,'series')
        payload=dict(source_id=ctx.source.source_id,path=path,kind='series',facts=facts,inputs=[native]);revision=digest(payload);old=head(ctx.connection,ctx.source.source_id,path,'series')
        if old and old['revision']==revision:continue
        identity=_subject_id(ctx,path,'series')
        item_catalog.record_capture(ctx.connection,item_id=identity,namespace=SOURCE,capture_key=revision,format_version=1,payload=payload)
        item_catalog.record_capture(ctx.connection,item_id=identity,namespace=HEAD,capture_key=str((old or {}).get('version',0)+1),format_version=1,payload=dict(source_id=ctx.source.source_id,path=path,kind='series',revision=revision,version=(old or {}).get('version',0)+1))




def preserve_raw(ctx,path,inputs,raws):
    """Materialize only bounded metadata bytes in the canonical preservation root."""
    from storage_layout import preserved_item_path,portable_path
    identity=_subject_id(ctx,path,'series');library=portable.collection_identity(ctx.connection,ctx.source)
    directory=preserved_item_path(ctx.suite_home,library,identity);result=[]
    for entry,raw in zip(inputs,raws):
        if len(raw)>formats.MAX_BYTES or hashlib.sha256(raw).hexdigest()!=entry['sha256']:raise formats.MetadataError('Original metadata changed.')
        name='ComicInfo.xml' if entry['format']=='comic-info' else 'details.json' if entry['format']=='details-json' else 'item.json'
        target=portable_path(directory,'originals/'+entry['sha256']+'/'+name,(),must_exist=False)
        target.parent.mkdir(parents=True,exist_ok=True);portable._create(target,raw)
        result.append(target)
    return result

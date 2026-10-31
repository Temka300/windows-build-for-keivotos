"""Video-owned saved metadata, explicit source refresh and personal field choices."""
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
CREATE TABLE IF NOT EXISTS video_metadata_overrides(
 source_id TEXT NOT NULL,path TEXT NOT NULL,kind TEXT NOT NULL,
 version INTEGER NOT NULL CHECK(version>0),fields_json TEXT NOT NULL,
 updated_at TEXT NOT NULL DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 PRIMARY KEY(source_id,path,kind));
CREATE TABLE IF NOT EXISTS video_metadata_updates(
 request_id TEXT PRIMARY KEY,source_id TEXT NOT NULL,path TEXT NOT NULL,kind TEXT NOT NULL,
 request_json TEXT NOT NULL,response_json TEXT NOT NULL);
"""
SOURCE='video.metadata.source'
HEAD='video.metadata.head'
PERSONAL='video.metadata.personal'
KINDS=('series','season','video')
EXTENSIONS=('mp4','m4v','webm','mkv','mov','avi','ogv')


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
    source=next((s for s in registered if s.source_id==source_id and s.visible and s.role=='video'),None)
    if source is None:raise HTTPException(404,'Video is unavailable.')
    return portable.Context(source,registered,Path(suite_home or config.SUITE_HOME),Path(index_path or config.FILES_DB_PATH),db,registry or config.MODULE_REGISTRY)


def video_rows(ctx,path=None,kind=None):
    from files_base import index
    with index.open_index(ctx.index_path) as db:
        scope,values=index.source_search_scope(db,ctx.registered,source_ids=[ctx.source.source_id],forbidden_paths=[ctx.suite_home])
        marks=','.join('?' for _ in EXTENSIONS)
        where=f'{scope} AND available=1 AND is_dir=0 AND ext IN ({marks})';values.extend(EXTENSIONS)
        if path is not None:
            if kind=='video':where+=' AND relative_path=?';values.append(path)
            else:where+=' AND relative_path>=? AND relative_path<?';values.extend([path+'/',path+'0'])
        return [dict(row) for row in db.execute('SELECT relative_path AS path,name,ext FROM files_index WHERE '+where+' ORDER BY relative_path',values)]


def validate_subject(ctx,path,kind):
    if kind not in KINDS:raise HTTPException(422,'Unknown detail scope.')
    try:
        from storage_layout import portable_relative
        portable_relative(path)
        if kind=='series' and '/' in path:raise ValueError('Series scope')
        if kind=='season' and '/' not in path:raise ValueError('Season scope')
        target=ctx.resolve(path)
        if (not target.is_file() if kind=='video' else not target.is_dir()) or not video_rows(ctx,path,kind):raise ValueError('Unavailable')
    except (OSError,ValueError,serving.ServeDenied) as error:raise HTTPException(404,'Video details are unavailable.') from error
    return target


def scopes(path,kind):
    parts=PurePosixPath(path).parts
    if kind=='series':return [(path,'series')]
    result=[(parts[0],'series')] if len(parts)>1 else []
    for length in range(2,len(parts)+(kind=='season')):result.append(('/'.join(parts[:length]),'season'))
    return [*result,(path,kind)] if not result or result[-1]!=(path,kind) else result


def _subject_id(ctx,path,kind):
    if not exists(ctx.connection,'kv_catalog_meta'):item_catalog.ensure_catalog_schema(ctx.connection)
    identity=str(uuid5(NAMESPACE_URL,'urn:keivotos:video:metadata:'+encoded([ctx.source.source_id,path,kind])))
    item_catalog.ensure_item(ctx.connection,namespace='video.metadata.subject',legacy_key=encoded([ctx.source.source_id,path,kind]),owner='video',item_type='metadata',item_id=identity)
    return identity


def saved(db,source_ids=None):
    result={}
    if not exists(db,'kv_item_captures'):return result
    cursor=db.cursor();cursor.row_factory=sqlite3.Row
    try:
        for row in cursor.execute("SELECT c.item_id,c.capture_key FROM kv_item_captures c JOIN kv_items i ON i.id=c.item_id WHERE c.namespace=? AND i.owner='video' AND c.capture_key=(SELECT CAST(MAX(CAST(h.capture_key AS INTEGER)) AS TEXT) FROM kv_item_captures h WHERE h.item_id=c.item_id AND h.namespace=c.namespace) ORDER BY c.item_id",(HEAD,)):
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
    identity=str(uuid5(NAMESPACE_URL,'urn:keivotos:video:metadata:'+encoded([source_id,path,kind])))
    row=db.execute('SELECT capture_key FROM kv_item_captures WHERE item_id=? AND namespace=? ORDER BY CAST(capture_key AS INTEGER) DESC LIMIT 1',(identity,HEAD)).fetchone()
    if not row:return None
    value=item_catalog.read_capture(db,item_id=identity,namespace=HEAD,capture_key=row['capture_key'])['payload']
    capture=item_catalog.read_capture(db,item_id=identity,namespace=SOURCE,capture_key=value['revision'])
    if not capture:raise formats.MetadataError('Saved details need review.')
    payload=capture['payload']
    return dict(facts=supplied(payload['facts']),version=value['version'],revision=value['revision'],inputs=payload['inputs'],recorded_at=capture['recorded_at'])


def personal(db,source_ids=None):
    if not exists(db,'video_metadata_overrides'):return {}
    cursor=db.cursor();cursor.row_factory=sqlite3.Row
    try:
        result={}
        for row in cursor.execute('SELECT * FROM video_metadata_overrides'):
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
    """Validate supported facts and their attachment before using native input."""
    extension=value['extensions'].get('video',{})
    if not isinstance(extension,dict):raise formats.MetadataError('Invalid Video manifest details.')
    envelope=extension.get('metadata',{})
    primary=[asset['path'] for asset in value['assets'] if asset['role']=='media']
    if not envelope:
        facts=formats.fields({k:v for k,v in extension.items() if k in formats.FIELDS})
        return [dict(path=path,kind='video',facts=facts) for path in primary] if facts else []
    if not isinstance(envelope,dict) or envelope.get('version')!=1 or not isinstance(envelope.get('subjects'),list) or len(envelope['subjects'])>512:raise formats.MetadataError('Unsupported Video metadata envelope.')
    from storage_layout import portable_relative
    result=[];seen=set()
    for entry in envelope['subjects']:
        if not isinstance(entry,dict) or set(entry)!={'path','kind','facts'} or entry['kind'] not in KINDS:raise formats.MetadataError('Invalid Video metadata subject.')
        path=entry['path'];kind=entry['kind']
        if not isinstance(path,str):raise formats.MetadataError('Invalid Video metadata path.')
        portable_relative(path)
        if (path,kind) in seen:raise formats.MetadataError('Duplicate Video metadata subject.')
        if not any(media==path if kind=='video' else media.startswith(path+'/') for media in primary):raise formats.MetadataError('Details are not attached to this item.')
        seen.add((path,kind));result.append(dict(path=path,kind=kind,facts=supplied(entry['facts'])))
    return result


def read_native(ctx,path,kind):
    folder=PurePosixPath(path) if kind!='video' else PurePosixPath(path).parent
    while True:
        relative=(folder/portable.MANIFEST).as_posix()
        candidate=ctx.resolve(relative,must_exist=False)
        if candidate.is_file():
            value,_=portable.read(ctx.source.path,relative,forbidden=ctx.forbidden,check_assets=False)
            if value['item']['owner']=='video':
                for entry in native_entries(value):
                    if (folder/entry['path']).as_posix()==path and entry['kind']==kind:
                        raw=portable._metadata_bytes(candidate)
                        return entry['facts'],dict(path=relative,format='native',item_id=value['item']['id'],sha256=hashlib.sha256(raw).hexdigest(),identity=portable.file_identity(candidate.stat())),raw
        if str(folder)=='.':break
        folder=folder.parent
    return {},None,None


def read_local(ctx,path,kind):
    validate_subject(ctx,path,kind)
    facts,native,native_raw=read_native(ctx,path,kind);inputs=[native] if native else [];observations=[]
    candidates=[]
    if kind=='series':candidates.append(((PurePosixPath(path)/'tvshow.nfo').as_posix(),'nfo'))
    elif kind=='season':candidates.append(((PurePosixPath(path)/'season.nfo').as_posix(),'nfo'))
    else:
        if '/' not in path and sum('/' not in r['path'] for r in video_rows(ctx))==1:candidates.append(('movie.nfo','nfo'))
        candidates.append((str(PurePosixPath(path).with_suffix('.nfo')),'nfo'))
    if kind!='video':candidates.append(((PurePosixPath(path)/'details.json').as_posix(),'details-json'))
    seen=set()
    for relative,format_name in candidates:
        if relative in seen:continue
        seen.add(relative);target=ctx.resolve(relative,must_exist=False)
        if not target.is_file():continue
        raw=portable._metadata_bytes(target)
        parsed=(formats.details_json if format_name=='details-json' else formats.nfo)(raw)
        observation=portable.observe(ctx.source.path,portable.asset(relative,role='metadata'),ctx.forbidden)
        if observation['sha256']!=hashlib.sha256(raw).hexdigest():raise formats.MetadataError('Local details changed while reading.')
        inputs.append(dict(path=relative,format=format_name,sha256=observation['sha256'],identity=observation['identity']))
        observations.append(observation);facts.update(parsed)
    return facts,inputs,observations,native_raw


def _fresh(ctx,path,kind,inputs):
    current=context(ctx.connection,ctx.source.source_id,index_path=ctx.index_path,suite_home=ctx.suite_home,registry=ctx.registry)
    if current.source!=ctx.source:raise HTTPException(409,'Video folder changed. Refresh details.')
    validate_subject(current,path,kind)
    for value in inputs:
        target=current.resolve(value['path'])
        if portable.file_identity(target.stat())!=tuple(value['identity']):raise HTTPException(409,'Local details changed. Refresh again.')


def refresh_one(ctx,path,kind):
    facts,inputs,observations,native_raw=read_local(ctx,path,kind)
    evidence=[{k:v for k,v in item.items() if k!='identity'} for item in inputs]
    payload=dict(source_id=ctx.source.source_id,path=path,kind=kind,facts=facts,inputs=evidence)
    revision=digest(payload);old=head(ctx.connection,ctx.source.source_id,path,kind)
    if old and old['revision']==revision:_fresh(ctx,path,kind,inputs);return False
    if not old and not facts and not inputs:return False
    identity=_subject_id(ctx,path,kind);library=portable.collection_identity(ctx.connection,ctx.source)
    assets=[portable.asset(Path(o['path']).relative_to(ctx.source.path).as_posix(),role='metadata',position=i,asset_id=str(uuid5(NAMESPACE_URL,revision+':'+str(i))),size=o['size'],sha256=o['sha256']) for i,o in enumerate(observations)]
    if assets or native_raw:
        if not assets:
            primary=video_rows(ctx,path,kind)[0]['path'];assets=[portable.asset(primary,role='media')]
        manifest=portable.new(item_id=identity,owner='video',item_type='metadata',library_id=library,storage='linked',assets=assets,extensions={'video':facts})
        portable.preserve_capture(ctx.suite_home,manifest,Path(ctx.source.path),observations if observations else [None],ctx.forbidden)
        if native_raw:portable.preserve_capture(ctx.suite_home,manifest,Path(ctx.source.path),observations if observations else [None],ctx.forbidden,metadata_raw=native_raw)
    # Reading originals and preserving bytes is complete before the authoritative acknowledgement.
    ctx.connection.execute('BEGIN IMMEDIATE')
    try:
        current=head(ctx.connection,ctx.source.source_id,path,kind)
        if (current or {}).get('revision')!=(old or {}).get('revision'):raise HTTPException(409,'Details were refreshed elsewhere. Retry.')
        _fresh(ctx,path,kind,inputs)
        item_catalog.record_capture(ctx.connection,item_id=identity,namespace=SOURCE,capture_key=revision,format_version=1,payload=payload)
        item_catalog.record_capture(ctx.connection,item_id=identity,namespace=HEAD,capture_key=str((old or {}).get('version',0)+1),format_version=1,
            payload=dict(source_id=ctx.source.source_id,path=path,kind=kind,revision=revision,version=(old or {}).get('version',0)+1))
        _fresh(ctx,path,kind,inputs);ctx.connection.commit()
    except BaseException:ctx.connection.rollback();raise
    return True


def refresh(ctx,path=None,kind=None):
    if path is not None:validate_subject(ctx,path,kind)
    selected=set()
    for row in video_rows(ctx,path,kind):selected.update(scopes(row['path'],'video'))
    updated=0
    for relative,scope in sorted(selected,key=lambda s:(len(PurePosixPath(s[0]).parts),s[0],s[1])):
        updated+=int(refresh_one(ctx,relative,scope))
    return dict(refreshed=updated,subjects=len(selected))


def after_rescan(source_id):
    from database import get_user_db
    from suite_modules import require_enabled
    import lifecycle
    with lifecycle.module_change_lock:
        require_enabled('video')
        with get_user_db() as db:return dict(video_metadata=refresh(context(db,source_id)))


def save_choices(ctx,path,kind,choices,version,source_revision,request_id):
    choices=formats.fields(choices,personal=True);validate_subject(ctx,path,kind)
    identity=_subject_id(ctx,path,kind)
    request=encoded(dict(source_id=ctx.source.source_id,path=path,kind=kind,fields=choices,version=version,source_revision=source_revision))
    ctx.connection.execute('BEGIN IMMEDIATE')
    try:
        prior=ctx.connection.execute('SELECT request_json,response_json FROM video_metadata_updates WHERE request_id=?',(request_id,)).fetchone()
        if prior:
            if prior['request_json']!=request:raise HTTPException(409,'This save request already has different details.')
            _fresh(ctx,path,kind,[]);ctx.connection.commit();return json.loads(prior['response_json'])
        current=details(ctx,path,kind)
        if current['version']!=version or current['source_revision']!=source_revision:raise HTTPException(409,'Details changed. Reload before saving.')
        if version>=9007199254740990:raise HTTPException(409,'Detail version needs review.')
        retained=ctx.connection.execute('SELECT fields_json FROM video_metadata_overrides WHERE source_id=? AND path=? AND kind=?',(ctx.source.source_id,path,kind)).fetchone()
        hidden={k:v for k,v in json.loads(retained['fields_json']).items() if k not in formats.FIELDS} if retained else {}
        stored={**hidden,**choices}
        ctx.connection.execute("""INSERT INTO video_metadata_overrides(source_id,path,kind,version,fields_json) VALUES(?,?,?,?,?)
            ON CONFLICT(source_id,path,kind) DO UPDATE SET version=excluded.version,fields_json=excluded.fields_json,
            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",(ctx.source.source_id,path,kind,version+1,encoded(stored)))
        response=details(ctx,path,kind)
        ctx.connection.execute('INSERT INTO video_metadata_updates VALUES(?,?,?,?,?,?)',(request_id,ctx.source.source_id,path,kind,request,encoded(response)))
        item_catalog.record_capture(ctx.connection,item_id=identity,namespace=PERSONAL,capture_key='save:'+request_id,format_version=1,
            payload=dict(source_id=ctx.source.source_id,path=path,kind=kind,version=version+1,fields=choices))
        _fresh(ctx,path,kind,[]);ctx.connection.commit();return response
    except BaseException:ctx.connection.rollback();raise


def import_catalog_records(db):
    """Independent metadata importer; originals and prior importer bodies stay intact."""
    if not db.in_transaction:raise ValueError('Import Video details inside a transaction')
    point='video_metadata_'+uuid4().hex;db.execute('SAVEPOINT '+point)
    cursor=db.cursor();cursor.row_factory=sqlite3.Row;count=0
    try:
        for table in ('video_metadata_overrides','video_metadata_updates'):
            if not exists(db,table):continue
            for row in cursor.execute('SELECT * FROM '+table):
                data=dict(row)
                if (not isinstance(data['source_id'],str) or not 0<len(data['source_id'])<=256 or '\0' in data['source_id'] or data['kind'] not in KINDS):raise ValueError('Invalid Video metadata identity')
                from storage_layout import portable_relative
                portable_relative(data['path'])
                if table.endswith('overrides'):
                    if type(data['version']) is not int or not 1<=data['version']<=9007199254740991:raise ValueError('Invalid metadata version')
                    supplied(json.loads(data['fields_json']),personal=True)
                else:
                    UUID(data['request_id'])
                    request=json.loads(data['request_json']);response=json.loads(data['response_json'])
                    if any(request[k]!=data[k] for k in ('source_id','path','kind')) or response['subject']!={k:data[k] for k in ('source_id','path','kind')}:raise ValueError('Invalid saved detail receipt')
                identity=str(uuid5(NAMESPACE_URL,'urn:keivotos:video:metadata:'+encoded([data['source_id'],data['path'],data['kind']])))
                item_catalog.ensure_item(db,namespace='video.metadata.subject',legacy_key=encoded([data['source_id'],data['path'],data['kind']]),owner='video',item_type='metadata',item_id=identity)
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
    entries=native_entries(value)
    folder=PurePosixPath(relative).parent.parent
    raw=portable._metadata_bytes(ctx.resolve(relative));native=dict(path=relative,format='native',item_id=value['item']['id'],sha256=hashlib.sha256(raw).hexdigest())
    for entry in entries:
        subpath=entry['path'];kind=entry['kind']
        path=(folder/subpath).as_posix();validate_subject(ctx,path,kind)
        payload=dict(source_id=ctx.source.source_id,path=path,kind=kind,facts=supplied(entry['facts']),inputs=[native]);revision=digest(payload)
        old=head(ctx.connection,ctx.source.source_id,path,kind)
        if old and old['revision']==revision:continue
        identity=_subject_id(ctx,path,kind)
        item_catalog.record_capture(ctx.connection,item_id=identity,namespace=SOURCE,capture_key=revision,format_version=1,payload=payload)
        item_catalog.record_capture(ctx.connection,item_id=identity,namespace=HEAD,capture_key=str((old or {}).get('version',0)+1),format_version=1,
            payload=dict(source_id=ctx.source.source_id,path=path,kind=kind,revision=revision,version=(old or {}).get('version',0)+1))
        item_catalog.relate_items(ctx.connection,source_item_id=identity,target_item_id=value['item']['id'],kind='details-source')

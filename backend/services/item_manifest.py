"""Portable item metadata, explicit preservation/export and owner dispatch.

No profile, original, catalog or optional module is opened at import time.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import unicodedata
from typing import Callable
from uuid import uuid4, uuid5, NAMESPACE_URL

from files_base import serving, sources
from services import item_catalog
from services.data_migrations import _attempt_lock
from storage_layout import portable_path, preserved_item_path

FORMAT = 'keivotos.item'
VERSION = 1
MAX_BYTES = 192 * 1024
MAX_ASSETS = 1024
MANIFEST = '.keivotos/item.json'
_KEYS = {'format','version','item','library','storage','origin','assets','context','extensions'}


class ManifestError(ValueError):
    """Refused metadata; all source and recovery evidence stays intact."""


@dataclass(frozen=True)
class PortableOwner:
    slug: str
    describe: Callable
    register: Callable | None = None


@dataclass(frozen=True)
class AssetRoots:
    # Map portable destination paths to explicitly authorized current inputs.
    # Only descriptor adapters construct this; manifests never supply authority.
    sources: dict

    def observe(self,info):
        root,relative,forbidden=self.sources[info['path']]
        return observe(root,{**info,'path':relative},forbidden)


@dataclass(frozen=True)
class Context:
    source: object
    registered: tuple
    suite_home: Path
    index_path: Path
    connection: sqlite3.Connection
    registry: object

    @property
    def forbidden(self):
        return [self.suite_home, *(Path(value.path) for value in
                sources.descendant_sources(self.source,self.registered))]

    def resolve(self, relative, *, must_exist=True):
        if not self.source.visible or self.source not in self.registered:
            raise ManifestError('Source is hidden or no longer registered')
        if relative == '':
            root=Path(self.source.path).absolute()
            if root.resolve()!=root:raise ManifestError('Linked source root')
            return serving.resolve_within_source(root,'',self.forbidden,must_exist=must_exist)
        return portable_path(self.source.path,relative,self.forbidden,must_exist=must_exist)


def _unique(pairs):
    result = {}
    for key,value in pairs:
        if key in result:
            raise ManifestError('Duplicate metadata key')
        result[key] = value
    return result


def _constant(value):
    raise ManifestError('Non-finite metadata value')


def encode(value):
    try:
        raw = (json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False,
                          separators=(',',':'))+'\n').encode('utf-8')
    except (TypeError,ValueError,UnicodeError,RecursionError) as exc:
        raise ManifestError('Invalid metadata value') from exc
    if len(raw)>MAX_BYTES:
        raise ManifestError('Item metadata exceeds its limit; preserve large source files as assets')
    return raw


def decode(raw):
    if not isinstance(raw,bytes) or len(raw)>MAX_BYTES:
        raise ManifestError('Item metadata exceeds its limit')
    try:
        value = json.loads(raw,object_pairs_hook=_unique,parse_constant=_constant)
    except (ValueError,UnicodeError,RecursionError) as exc:
        raise ManifestError('Invalid item metadata') from exc
    return validate(value)


def _identity(value, label):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',value):
        raise ManifestError('Invalid '+label)
    from storage_layout import portable_relative
    portable_relative(value)
    return value


def _name(value, label):
    if not isinstance(value,str) or not re.fullmatch(r'[a-z][a-z0-9_.:-]{0,63}',value):
        raise ManifestError('Invalid '+label)
    return value


def validate(value):
    if not isinstance(value,dict) or set(value)!=_KEYS:
        raise ManifestError('Unsupported item metadata fields')
    if value['format']!=FORMAT or type(value['version']) is not int or value['version']!=VERSION:
        raise ManifestError('Unsupported item metadata version')
    item,library=value['item'],value['library']
    if not isinstance(item,dict) or set(item)!={'id','owner','type'}:
        raise ManifestError('Invalid item identity')
    _identity(item['id'],'item ID');_name(item['owner'],'owner');_name(item['type'],'item type')
    if not isinstance(library,dict) or set(library)!={'id','kind','name'}:
        raise ManifestError('Invalid library identity')
    _identity(library['id'],'library ID')
    if library['kind'] not in ('managed','linked') or not isinstance(library['name'],str) or len(library['name'])>1024:
        raise ManifestError('Invalid library facts')
    if value['storage'] not in ('managed','linked'):
        raise ManifestError('Invalid item storage policy')
    for field in ('origin','context','extensions'):
        if not isinstance(value[field],dict):
            raise ManifestError('Invalid '+field)
    assets=value['assets']
    if not isinstance(assets,list) or not 1<=len(assets)<=MAX_ASSETS:
        raise ManifestError('Invalid asset selection')
    paths,identities,positions={},{},set()
    for asset in assets:
        if not isinstance(asset,dict) or set(asset)!={'id','role','position','path','size','sha256','media_type','required'}:
            raise ManifestError('Invalid asset fields')
        _identity(asset['id'],'asset ID');_name(asset['role'],'asset role')
        if type(asset['position']) is not int or asset['position']<0 or (asset['role'],asset['position']) in positions:
            raise ManifestError('Duplicate or invalid asset position')
        positions.add((asset['role'],asset['position']))
        # Validate portable syntax without resolving any filesystem path.
        from storage_layout import portable_relative
        path=portable_relative(asset['path'])
        if (path.casefold()==MANIFEST or path.casefold().startswith('.keivotos/history/') or
                path.casefold()=='.keivotos/export-request.json'):
            raise ManifestError('Item assets cannot reference their own preservation metadata')
        folded=unicodedata.normalize('NFC',path).casefold()
        if folded in paths and paths[folded]!=path:
            raise ManifestError('Portable asset paths collide by case or Unicode')
        paths[folded]=path
        if asset['size'] is not None and (type(asset['size']) is not int or not 0<=asset['size']<=2**63-1):
            raise ManifestError('Invalid asset size')
        if asset['sha256'] is not None and (not isinstance(asset['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',asset['sha256'])):
            raise ManifestError('Invalid asset hash')
        if not isinstance(asset['media_type'],str) or not 1<=len(asset['media_type'])<=256 or type(asset['required']) is not bool:
            raise ManifestError('Invalid asset type or availability policy')
        facts=(path,asset['size'],asset['sha256'],asset['media_type'])
        if asset['id'] in identities and identities[asset['id']]!=facts:
            raise ManifestError('Asset identity has conflicting facts')
        identities[asset['id']]=facts
    context=value['context']
    nodes=context.get('parents',[])
    relations=context.get('relations',[])
    if not isinstance(nodes,list) or len(nodes)>128 or not isinstance(relations,list) or len(relations)>512:
        raise ManifestError('Item context is too large')
    known={item['id']}
    for node in nodes:
        if not isinstance(node,dict) or not {'id','owner','type'}<=node.keys():
            raise ManifestError('Invalid parent context')
        _identity(node['id'],'parent ID');_name(node['owner'],'parent owner');_name(node['type'],'parent type')
        if node['id'] in known:
            raise ManifestError('Duplicate context identity')
        known.add(node['id'])
    graph={key:[] for key in known}
    for relation in relations:
        if not isinstance(relation,dict) or set(relation)!={'from','to','kind','position'}:
            raise ManifestError('Invalid context relationship')
        if relation['from'] not in known or relation['to'] not in known or type(relation['position']) is not int or relation['position']<0:
            raise ManifestError('Unknown context relationship')
        _name(relation['kind'],'relationship kind')
        graph[relation['from']].append(relation['to'])
    visited,active=set(),set()
    def visit(key):
        if key in active:raise ManifestError('Cyclic item context')
        if key in visited:return
        active.add(key)
        for target in graph[key]:visit(target)
        active.remove(key);visited.add(key)
    for key in known:visit(key)
    # Bound nested unknown source facts as well as their encoded byte size.
    item_catalog._capture_payload(value)
    encode(value)
    return value


def new(*, item_id, owner, item_type, library_id, assets, library_name='',
        library_kind='linked', storage='managed', origin=None, context=None, extensions=None):
    return validate(dict(format=FORMAT,version=VERSION,
        item=dict(id=item_id,owner=owner,type=item_type),
        library=dict(id=library_id,kind=library_kind,name=library_name),
        storage=storage,origin=origin or {},assets=assets,context=context or {},extensions=extensions or {}))


def file_identity(value):
    return value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns


def observe(root,asset,forbidden=(),*,hash_bytes=True):
    try:
        path=portable_path(root,asset['path'],forbidden)
    except serving.ServeDenied as exc:
        if exc.status_code==404 and not asset['required']:return None
        raise ManifestError(exc.detail) from exc
    if not path.is_file():raise ManifestError('Asset is not a regular file')
    before=path.stat()
    if asset['size'] is not None and before.st_size!=asset['size']:
        raise ManifestError('Asset size changed')
    digest=None
    if hash_bytes:
        with path.open('rb') as stream:
            if file_identity(os.fstat(stream.fileno()))!=file_identity(before):
                raise ManifestError('Asset changed before validation')
            digest=hashlib.file_digest(stream,'sha256').hexdigest()
            if file_identity(os.fstat(stream.fileno()))!=file_identity(before):
                raise ManifestError('Asset changed during validation')
        if asset['sha256'] is not None and asset['sha256']!=digest:
            raise ManifestError('Asset hash changed')
    if file_identity(path.stat())!=file_identity(before):
        raise ManifestError('Asset changed after validation')
    portable_path(root,asset['path'],forbidden)
    return dict(path=str(path),size=before.st_size,sha256=digest,identity=file_identity(before))


def read(root,relative=MANIFEST,*,forbidden=(),asset_root=None,hash_assets=False,check_assets=True):
    if relative!=MANIFEST and not relative.endswith('/'+MANIFEST):
        raise ManifestError('Invalid item metadata location')
    path=portable_path(root,relative,forbidden)
    with path.open('rb') as stream:
        before=file_identity(os.fstat(stream.fileno()));raw=stream.read(MAX_BYTES+1)
        if file_identity(os.fstat(stream.fileno()))!=before:
            raise ManifestError('Item metadata changed while reading')
    value=decode(raw)
    if file_identity(path.stat())!=before:raise ManifestError('Item metadata changed after reading')
    root=Path(asset_root) if asset_root is not None else path.parent.parent
    observations=[observe(root,asset,forbidden,hash_bytes=hash_assets) for asset in value['assets']] if check_assets else []
    portable_path(path.parent,path.name,forbidden)
    return value,observations


def _flush_directory(path):
    if os.name=='nt':return
    descriptor=os.open(path,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
    try:os.fsync(descriptor)
    finally:os.close(descriptor)


def _create(path,raw):
    path=Path(path)
    portable_path(path.parent,path.name,(),must_exist=False)
    if path.exists():
        with path.open('rb') as stream:existing=stream.read(len(raw)+1)
        if existing!=raw:raise ManifestError('Existing output differs; preserved without replacement')
        return
    temporary=path.with_name('.'+path.name+'.'+uuid4().hex+'.partial')
    with temporary.open('xb') as stream:
        stream.write(raw);stream.flush();os.fsync(stream.fileno())
    try:os.link(temporary,path)
    except FileExistsError:
        with path.open('rb') as stream:existing=stream.read(len(raw)+1)
        if existing!=raw:raise ManifestError('Concurrent output conflicts')
    # Only this successful generated temporary is removed. A failed operation
    # retains its partial file and all complete/source/history evidence.
    temporary.unlink()
    _flush_directory(path.parent)



def write(root,value,*,refresh=False,forbidden=()):
    """Publish this metadata only; caller owns its item's completion boundary."""
    value=validate(value);root=Path(root)
    directory=portable_path(root,'.keivotos',forbidden,must_exist=False)
    directory.mkdir(exist_ok=True)
    path=portable_path(directory,'item.json',forbidden,must_exist=False);raw=encode(value)
    with _attempt_lock(directory):
        previous=_metadata_bytes(path) if path.exists() else None
        if previous is not None and previous!=raw:
            prior=decode(previous)
            if (not refresh or prior['item']!=value['item'] or
                    any(prior['library'][key]!=value['library'][key] for key in ('id','kind')) or
                    prior['storage']!=value['storage']):
                raise ManifestError('Existing item metadata conflicts')
            history=portable_path(directory,'history',(),must_exist=False);history.mkdir(exist_ok=True)
            _create(history/(hashlib.sha256(previous).hexdigest()+'.json'),previous)
            temporary=directory/(uuid4().hex+'.refresh.partial')
            _create(temporary,raw)
            portable_path(directory,'item.json',())
            if _metadata_bytes(path)!=previous:raise ManifestError('Metadata changed during refresh')
            os.replace(temporary,path)
        else:_create(path,raw)
    _flush_directory(directory)
    _flush_directory(root)
    return hashlib.sha256(raw).hexdigest()


def preserve(home,value,*,refresh=False):
    """Explicit linked metadata preservation; writes only in the data home."""
    directory=preserved_item_path(home,value['library']['id'],value['item']['id'])
    directory.mkdir(parents=True,exist_ok=True)
    return write(directory,{**value,'storage':'linked'},refresh=refresh)


def asset(path,*,role='media',position=0,asset_id=None,size=None,sha256=None,media_type=None,required=True):
    return dict(id=asset_id or str(uuid4()),role=role,position=position,path=path,
                size=size,sha256=sha256,media_type=media_type or serving.inline_media_type(Path(path))[0],required=required)


def ensure_identity(connection,source,relative,owner,item_type,*,item_id=None):
    """Explicit selection only; existing catalogs and aliases keep their IDs."""
    library=collection_identity(connection,source)
    identity=item_catalog.ensure_item(connection,namespace='portable.'+owner,
        legacy_key=json.dumps([source.source_id,relative,item_type],ensure_ascii=False,separators=(',',':')),
        owner=owner,item_type=item_type,item_id=item_id)
    return library,identity


def register(value,context,relative,*,observations=None,register_owner=True):
    """Register one verified item in a caller transaction, independently of jobs."""
    value=validate(value)
    descriptor=context.registry.get(value['item']['owner'])
    if descriptor is None or descriptor.portable_provider is None:
        raise ManifestError('Portable owner is not installed')
    if (any(part.casefold()=='.keivotos' for part in Path(relative).parts[:-2]) or
            any(part.casefold()=='.keivotos' for part in Path(context.source.path).parts)):
        raise ManifestError('Managed staging is not a published portable item')
    metadata_path=context.resolve(relative)
    metadata_raw=_metadata_bytes(metadata_path)
    if decode(metadata_raw)!=value:raise ManifestError('Registration metadata differs from its source file')
    root_relative=str(Path(relative).parent.parent.as_posix())
    if root_relative=='.':root_relative=''
    root=context.resolve(root_relative)
    if observations is None:
        observations=[observe(root,a,context.forbidden) for a in value['assets']]
    db=context.connection
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='kv_catalog_meta'").fetchone():
        item_catalog.ensure_catalog_schema(db)
    if observations is None or len(observations)!=len(value['assets']):
        raise ManifestError('Incomplete asset verification')
    if any(o is not None and not o.get('sha256') for o in observations):
        raise ManifestError('Registration requires explicit byte verification')
    with item_catalog._write(db):
        item=value['item'];library=value['library']
        for binding in bindings(db,source_id=context.source.source_id):
            if binding['path']==relative and binding['item_id']!=item['id']:
                raise ManifestError('Another portable identity already occupies this location')
        # A portable library is provenance. It never reclassifies a registered
        # linked root or grants permission to serve the old captured location.
        item_catalog.ensure_library(db,namespace='portable.library',legacy_key=library['id'],
            kind=library['kind'],display_name=library['name'] or library['id'],library_id=library['id'])
        collection_identity(db,context.source)
        item_catalog.ensure_item(db,namespace='portable.identity',legacy_key=item['id'],
            owner=item['owner'],item_type=item['type'],item_id=item['id'])
        for parent in value['context'].get('parents',[]):
            item_catalog.ensure_item(db,namespace='portable.identity',legacy_key=parent['id'],
                owner=parent['owner'],item_type=parent['type'],item_id=parent['id'])
        for relation in value['context'].get('relations',[]):
            cyclic=db.execute('''WITH RECURSIVE reachable(id) AS (
                SELECT ? UNION SELECT r.target_item_id FROM kv_item_relations r
                    JOIN reachable n ON r.source_item_id=n.id)
                SELECT 1 FROM reachable WHERE id=? LIMIT 1''',(relation['to'],relation['from'])).fetchone()
            if cyclic:raise ManifestError('Cyclic retained item relationships')
            item_catalog.relate_items(db,source_item_id=relation['from'],target_item_id=relation['to'],
                kind=relation['kind'],position=relation['position'])
        for asset_value,observation in zip(value['assets'],observations):
            if observation is None:continue
            item_catalog.record_asset(db,asset_id=asset_value['id'],media_type=asset_value['media_type'],
                byte_size=observation['size'],hash_algorithm='sha256',content_hash=observation['sha256'])
            item_catalog.link_asset(db,item_id=item['id'],asset_id=asset_value['id'],
                role=asset_value['role'],position=asset_value['position'])
        preserve_capture(context.suite_home,value,root,observations,context.forbidden,metadata_raw=metadata_raw)
        digest=hashlib.sha256(encode(value)).hexdigest()
        item_catalog.record_capture(db,item_id=item['id'],namespace='portable.manifest',
            capture_key=digest,format_version=VERSION,payload=value)
        binding=dict(source_id=context.source.source_id,path=relative,root=root_relative,digest=digest)
        key=hashlib.sha256(encode(binding)).hexdigest()
        item_catalog.record_capture(db,item_id=item['id'],namespace='portable.location',
            capture_key=key,format_version=VERSION,payload=binding)
        callback=descriptor.portable_provider().register
        if register_owner and callback is not None:callback(value,context,relative)
        for asset_value,observation in zip(value['assets'],observations):
            current=observe(root,asset_value,context.forbidden,hash_bytes=False)
            if (current is None)!=(observation is None) or current is not None and (
                    current['identity']!=observation['identity'] or current['path']!=observation['path']):
                raise ManifestError('Original changed before registration acknowledgement')
        if _metadata_bytes(context.resolve(relative))!=metadata_raw:
            raise ManifestError('Metadata changed before registration acknowledgement')
    return item['id']


def import_item(context,relative=MANIFEST):
    context.resolve(relative)
    value,observations=read(context.source.path,relative,forbidden=context.forbidden,hash_assets=True)
    return register(value,context,relative,observations=observations)


def export_item(context,selection,destination,*,request_id,options=None):
    """Explicit verified single-item copy; a completion manifest is installed last."""
    _identity(request_id,'export request')
    descriptor=context.registry.get(context.source.role)
    if descriptor is None or descriptor.portable_provider is None:
        raise ManifestError('Source owner does not support portable items')
    value,root=descriptor.portable_provider().describe(context,selection,options or {})
    value=validate({**value,'storage':'managed'})
    def selected(info):
        return root.observe(info) if isinstance(root,AssetRoots) else observe(Path(root),info,context.forbidden)
    destination=Path(destination).absolute()
    # Destination is explicit, new, outside the source and outside app data.
    if destination.resolve()!=destination or destination.is_relative_to(Path(context.source.path).resolve()) or destination.is_relative_to(context.suite_home.resolve()):
        raise ManifestError('Choose a new export folder outside source and app data')
    destination.parent.mkdir(parents=True,exist_ok=True)
    portable_path(destination.parent,destination.name,(),must_exist=False)
    stage=destination.with_name('.'+destination.name+'.export-'+request_id)
    portable_path(stage.parent,stage.name,(),must_exist=False)
    stage.mkdir(exist_ok=True)
    with _attempt_lock(stage):
        snapshots=[selected(a) for a in value['assets']]
        value={**value,'assets':[{**a,'size':o['size'],'sha256':o['sha256']} if o else a
                               for a,o in zip(value['assets'],snapshots)]}
        validate(value)
        intent=dict(destination=str(destination),manifest_sha256=hashlib.sha256(encode(value)).hexdigest(),
                    sources=[o['identity'] if o else None for o in snapshots])
        _create(stage/'intent.json',encode(intent))
        for info,observation in zip(value['assets'],snapshots):
            if observation is None:continue
            target=portable_path(stage,info['path'],(),must_exist=False)
            target.parent.mkdir(parents=True,exist_ok=True)
            copy_observation(observation,target)
            actual=observe(stage,info)
            if actual['sha256']!=observation['sha256'] or actual['size']!=observation['size']:
                raise ManifestError('Exported asset verification failed')
            current=selected(info)
            if current!=observation:raise ManifestError('Original changed during export')
        write(stage,value)
        published=destination/'.keivotos/item.json'
        marker=stage/'publication.json'
        receipt=encode(dict(destination=str(destination),manifest_sha256=hashlib.sha256(encode(value)).hexdigest()))
        if destination.exists() and published.exists():
            existing,_=read(destination,hash_assets=True)
            if existing!=value:raise ManifestError('Export destination already contains another item')
            return destination
        if destination.exists():
            claim=portable_path(destination,'.keivotos/export-request.json',(),must_exist=False)
            if not marker.exists() or marker.read_bytes()!=receipt or not claim.exists() or claim.read_bytes()!=receipt:
                raise ManifestError('Export destination already exists without this publication receipt')
        else:
            # The journal is retained before creating the destination. The
            # exclusive mkdir remains the no-replacement publication fence.
            _create(marker,receipt)
            destination.mkdir()
            claim_dir=portable_path(destination,'.keivotos',(),must_exist=False);claim_dir.mkdir()
            _create(claim_dir/'export-request.json',receipt)
        for info,observation in zip(value['assets'],snapshots):
            if observation is None:continue
            target=portable_path(destination,info['path'],(),must_exist=False)
            target.parent.mkdir(parents=True,exist_ok=True)
            if target.exists():
                if observe(destination,info)['sha256']!=observation['sha256']:
                    raise ManifestError('Interrupted export contains conflicting bytes')
            else:os.link(stage/info['path'],target)
        write(destination,value)
        read(destination,hash_assets=True)
    return destination


def stable_asset(item_id, path, **facts):
    return asset(path,asset_id=str(uuid5(NAMESPACE_URL,'urn:keivotos:asset:'+item_id+':'+path)),**facts)


def local_description(context, selection, options, *, owner, item_type, facts=None, paths=None):
    """Describe an explicit local selection, never scan/reorganize a collection."""
    if not isinstance(selection,str) or not isinstance(options,dict) or set(options)-{'related','parent','origin'}:
        raise ManifestError('Invalid portable selection')
    context.resolve(selection)
    if not options:
        existing=existing_description(context,selection,owner)
        if existing is not None:return existing
    library,identity=ensure_identity(context.connection,context.source,selection,owner,item_type)
    related=options.get('related',[])
    if not isinstance(related,list) or len(related)>128:
        raise ManifestError('Too many explicitly selected related assets')
    selected=paths or [(selection,'media')]
    selected=[*selected,*[(p,'related') for p in related]]
    assets=[];positions={}
    # Keep selected relative paths. This copies only the selected item, never
    # establishes a mandatory publisher/album layout in its original library.
    for path,role in selected:
        target=context.resolve(path)
        if not target.is_file():raise ManifestError('Selected asset is not a file')
        position=positions.get(role,0);positions[role]=position+1
        assets.append(stable_asset(identity,path,role=role,position=position,size=target.stat().st_size))
    parents=[];relations=[]
    parent=options.get('parent')
    if parent is not None:
        if not isinstance(parent,dict) or not {'id','owner','type'}<=parent.keys():
            raise ManifestError('Explicit parent context needs its real stable identity')
        parents=[parent];relations=[dict(**{'from':identity,'to':parent['id']},kind='part-of',position=0)]
    value=new(item_id=identity,owner=owner,item_type=item_type,library_id=library,
        library_name=context.source.display_name,storage='linked',
        origin=options.get('origin',{}),assets=assets,
        context={'parents':parents,'relations':relations},
        extensions={owner:facts or {}})
    return value,Path(context.source.path)


def bindings(connection, *, owner=None, source_id=None):
    """Read retained locations without treating their provenance as authority."""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='kv_item_captures'").fetchone():return []
    cursor=connection.cursor();cursor.row_factory=sqlite3.Row
    try:
        rows=cursor.execute('''SELECT c.item_id,c.capture_key FROM kv_item_captures c
            JOIN kv_items i ON i.id=c.item_id WHERE c.namespace='portable.location'
            AND (? IS NULL OR i.owner=?) ORDER BY c.rowid DESC''',(owner,owner)).fetchall()
        result=[];seen=set()
        for row in rows:
            capture=item_catalog.read_capture(connection,item_id=row['item_id'],
                namespace='portable.location',capture_key=row['capture_key'])
            value=capture['payload']
            location=(row['item_id'],value['source_id'],value['path'])
            if location in seen:continue
            seen.add(location)
            if source_id is None or value['source_id']==source_id:
                result.append(dict(item_id=row['item_id'],**value))
        return result
    finally:cursor.close()


def discover(context):
    """Explicit registration/rescan repair; no writes in originals, no startup use.

    Walk only metadata names; never follow links, enter private app data or
    registered children. Each selected import validates its current bytes.
    Failures are returned independently, retaining complete unregistered items.
    """
    root=context.resolve('')
    results={'imported':[],'refused':[]}
    for directory,dirs,files in os.walk(root,followlinks=False):
        base=Path(directory)
        admitted=[]
        for name in dirs:
            candidate=base/name
            if name.startswith('.') and name!='.keivotos':continue
            try:
                relative=candidate.relative_to(root).as_posix()
                context.resolve(relative)
            except (ValueError,OSError,serving.ServeDenied):continue
            admitted.append(name)
        dirs[:]=admitted
        if base.name!='.keivotos':continue
        # Completion metadata is the only discovered entry in this reserved
        # directory. Never descend into acquisition staging or metadata history.
        dirs[:]=[]
        if 'item.json' not in files:continue
        relative=(base/'item.json').relative_to(root).as_posix()
        try:
            value,_=read(root,relative,forbidden=context.forbidden)
            if value['item']['owner'] not in ('files',context.source.role):continue
            identity=import_item(context,relative)
            results['imported'].append(identity)
        except (ValueError,OSError,serving.ServeDenied,sqlite3.Error) as exc:
            results['refused'].append({'path':relative,'reason':str(exc)})
    return results


def scan_registered(source, registered, suite_home, index_path, connection, registry):
    descriptor=registry.get(source.role)
    if not source.visible or descriptor is None or descriptor.portable_provider is None:return {'imported':[],'refused':[]}
    return discover(Context(source,tuple(registered),Path(suite_home),Path(index_path),connection,registry))


def preserve_capture(home,value,root,observations,forbidden=(),*,metadata_raw=None):
    """Keep required source facts and original artwork eligible for user backups.

    A preservation record is not a self-contained media export. Its relative
    media references still describe the linked item; item 06 owns bulk transfer.
    """
    directory=preserved_item_path(home,value['library']['id'],value['item']['id'])
    directory.mkdir(parents=True,exist_ok=True)
    for a,o in zip(value['assets'],observations):
        if o is None or a['role'] not in ('source','metadata','manifest','cover','thumbnail','avatar'):continue
        stored='originals/'+o['sha256']+'/'+a['path']
        target=portable_path(directory,stored,(),must_exist=False)
        target.parent.mkdir(parents=True,exist_ok=True)
        copy_observation(o,target)
        if observe(root,a,forbidden)!=o:raise ManifestError('Source changed during preservation')
    # Distinct metadata revisions are immutable; never replace captured evidence.
    revisions=portable_path(directory,'captures',(),must_exist=False);revisions.mkdir(exist_ok=True)
    raw=metadata_raw if metadata_raw is not None else encode(value)
    _create(revisions/(hashlib.sha256(raw).hexdigest()+'.json'),raw)


def existing_description(context,selection,owner):
    target=context.resolve(selection)
    root=context.resolve('')
    folder=target if target.is_dir() else target.parent
    while folder==root or root in folder.parents:
        candidate=folder/MANIFEST
        relative=candidate.relative_to(root).as_posix()
        context.resolve(relative,must_exist=False)
        if candidate.is_file():
            value,_=read(root,relative,forbidden=context.forbidden)
            if value['item']['owner']==owner:
                selected=target.relative_to(folder).as_posix()
                primary=[a for a in value['assets'] if a['role'] in ('media','archive','page')]
                if any(a['path']==selected or target.is_dir() and
                       (selected=='.' or a['path'].startswith(selected+'/')) for a in primary):
                    return value,folder
        if folder==root:break
        folder=folder.parent
    return None


def copy_observation(observation,target):
    target=Path(target)
    expected=asset(target.name,asset_id='copy',size=observation['size'],sha256=observation['sha256'])
    portable_path(target.parent,target.name,(),must_exist=False)
    if target.exists():
        observe(target.parent,expected)
        return
    temporary=target.with_name('.'+target.name+'.'+uuid4().hex+'.partial')
    digest=hashlib.sha256();size=0
    with Path(observation['path']).open('rb') as source,temporary.open('xb') as output:
        if file_identity(os.fstat(source.fileno()))!=tuple(observation['identity']):
            raise ManifestError('Original changed before copying')
        while chunk:=source.read(min(1024*1024,observation['size']-size+1)):
            size+=len(chunk)
            if size>observation['size']:raise ManifestError('Original grew during copying')
            digest.update(chunk);output.write(chunk)
        output.flush();os.fsync(output.fileno())
        if file_identity(os.fstat(source.fileno()))!=tuple(observation['identity']):
            raise ManifestError('Original changed while copying')
    if size!=observation['size'] or digest.hexdigest()!=observation['sha256']:
        raise ManifestError('Copied bytes do not match the selected original')
    observe(temporary.parent,{**expected,'path':temporary.name})
    portable_path(target.parent,target.name,(),must_exist=False)
    try:os.link(temporary,target)
    except FileExistsError:observe(target.parent,expected)
    temporary.unlink()
    _flush_directory(target.parent)


def collection_identity(connection,source):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='kv_catalog_meta'").fetchone():
        item_catalog.ensure_catalog_schema(connection)
    existing=item_catalog.library_for_legacy(connection,namespace='files.source',legacy_key=source.source_id)
    return item_catalog.ensure_library(connection,namespace='files.source',legacy_key=source.source_id,
        kind='linked',display_name=source.display_name,
        library_id=existing['id'] if existing is not None else source.source_id)


def _metadata_bytes(path):
    with path.open('rb') as stream:
        before=file_identity(os.fstat(stream.fileno()))
        if before[2]>MAX_BYTES:raise ManifestError('Item metadata exceeds its limit')
        raw=stream.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES or file_identity(os.fstat(stream.fileno()))!=before:
            raise ManifestError('Metadata changed during bounded read')
    if file_identity(path.stat())!=before:raise ManifestError('Metadata changed after bounded read')
    return raw

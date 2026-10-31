"""YouTube's portable envelope; legacy capture facts remain owner validated."""
import hashlib
import json
from pathlib import Path
from uuid import uuid5, NAMESPACE_URL
from services import item_manifest as portable, item_catalog

def identity(job_id):
    return str(uuid5(NAMESPACE_URL,'urn:keivotos:youtube:download:'+job_id))

def envelope(directory,job,value,*,snapshot=None):
    item_id=identity(job['id'])
    assets=[]
    for role,path in [('media',value['media']),('thumbnail',value['thumbnail']),('avatar',value['capture']['avatar'])]:
        if not path:continue
        asset_id=str(uuid5(NAMESPACE_URL,'urn:keivotos:youtube:asset:'+
                          json.dumps([item_id,role],separators=(',',':'))))
        selected=portable.asset(path,role=role,asset_id=asset_id,required=role=='media')
        observed=portable.observe(directory,selected)
        selected.update(size=observed['size'],sha256=observed['sha256'])
        assets.append(selected)
    if snapshot is not None:
        from modules.youtube import details
        details.validate(snapshot,value['capture'])
        if snapshot['description'] is not None:
            raw=details.asset_bytes(directory,snapshot)
            asset_id=str(uuid5(NAMESPACE_URL,'urn:keivotos:youtube:asset:'+json.dumps(
                [item_id,'metadata'],separators=(',',':'))))
            assets.append(portable.asset(snapshot['description']['path'],role='metadata',asset_id=asset_id,
                size=len(raw),sha256=hashlib.sha256(raw).hexdigest(),media_type='text/plain',required=False))
    extension=dict(saved=value,request={**{key:job.get(key,'') for key in
        ('video','audio','created_at')},'original_title':job.get('portable_title',value['title'])})
    if snapshot is not None:extension['details']=snapshot
    return portable.new(item_id=item_id,owner='youtube',item_type='saved-media',
        library_id=job.get('portable_library_id') or str(uuid5(NAMESPACE_URL,'urn:keivotos:source:'+job['source_id'])),
        library_name=job.get('source_name') or 'YouTube',assets=assets,
        origin={'url':value['source_url'],'captured_at':value['capture']['captured_at']},
        extensions={'youtube':extension})

def saved(value):
    portable.validate(value)
    if value['item']['owner']!='youtube' or value['item']['type']!='saved-media':
        raise ValueError('Saved portable owner changed')
    extension=value['extensions'].get('youtube')
    if not isinstance(extension,dict) or set(extension) not in ({'saved','request'},{'saved','request','details'}):
        raise ValueError('Missing YouTube capture facts')
    old=extension['saved']
    request=extension['request']
    if (not isinstance(request,dict) or set(request)!={'video','audio','created_at','original_title'}
            or any(not isinstance(v,str) for v in request.values())):
        raise ValueError('Invalid portable YouTube request facts')
    if (not isinstance(old,dict) or set(old)!={'version','saved_item_id','source_url','title','media','thumbnail','capture'}
            or old.get('version')!=1 or value['item']['id']!=identity(old['saved_item_id'])):
        raise ValueError('Saved portable identity changed')
    from modules.youtube import metadata
    if metadata.text(request['original_title'])!=old['title']:
        raise ValueError('Captured title relationship changed')
    expected={'media':old['media']}
    if old['thumbnail']:expected['thumbnail']=old['thumbnail']
    if old['capture'].get('avatar'):expected['avatar']=old['capture']['avatar']
    if 'details' in extension:
        from modules.youtube import details
        snapshot=details.validate(extension['details'],old['capture'])
        if snapshot['description'] is not None:expected['metadata']=snapshot['description']['path']
    actual={a['role']:a['path'] for a in value['assets']}
    if actual!=expected or len(actual)!=len(value['assets']):
        raise ValueError('Saved portable asset relationships changed')
    return old

def register(value,context,relative):
    from modules.youtube.user_schema import register_catalog_publication
    extension=value['extensions'].get('youtube',{})
    physical_digest=hashlib.sha256(portable._metadata_bytes(context.resolve(relative))).hexdigest()
    if 'legacy' in extension:
        if set(extension)-{'details'} not in ({'legacy'},{'legacy','legacy_manifest'}):raise ValueError('Invalid legacy saved context')
        root_relative=Path(relative).parent.parent.as_posix()
        prefix='' if root_relative=='.' else root_relative
        record,owners=legacy_record(value,context.source.source_id,context.source.path,prefix,manifest_digest=physical_digest)
        return _register(context,record,owners)
    old=saved(value);request=extension['request']
    if not isinstance(request,dict) or set(request)!={'video','audio','created_at','original_title'}:
        raise ValueError('Missing saved request facts')
    folder=Path(relative).parent.parent.as_posix()
    if folder=='.':folder=''
    def join(name):return '/'.join(p for p in (folder,name) if p)
    facts={**old['capture']}
    if facts['avatar']:facts['avatar']=join(facts['avatar'])
    record=dict(id=old['saved_item_id'],url=old['source_url'],source_id=context.source.source_id,
        root=context.source.path,path=join(old['media']),thumbnail=join(old['thumbnail']) if old['thumbnail'] else '',
        title=request['original_title'],**{key:request[key] for key in ('video','audio','created_at')},
        **facts,layout_version=1,bundle_path=folder,
        manifest_sha256=physical_digest,retry_of='')
    return _register(context,record,[record['id']])

def describe(context,selection,options):
    if options:raise ValueError('A saved YouTube item already includes its captured context')
    target=context.resolve(selection)
    folder=target if target.is_dir() else target.parent
    candidate=folder/portable.MANIFEST
    relative=candidate.relative_to(context.source.path).as_posix()
    if context.resolve(relative,must_exist=False).exists():
        raw=portable._metadata_bytes(candidate)
        parsed=json.loads(raw,object_pairs_hook=portable._unique)
        if isinstance(parsed,dict) and parsed.get('format')==portable.FORMAT:
            value=portable.decode(raw)
            if 'legacy' in value['extensions'].get('youtube',{}):return value,folder
            original=saved(value)
            from modules.youtube import metadata
            selected=metadata._preserved_file_capture(context.connection,context.source.source_id,
                folder.relative_to(context.source.path).as_posix())
            if selected is None or selected['payload']['record']['id']==original['saved_item_id']:
                return value,folder
    from modules.youtube import metadata
    paths=[selection,folder.relative_to(context.source.path).as_posix()]
    if target.is_dir():
        from files_base import index
        parent=target.relative_to(context.source.path).as_posix()
        with index.open_index(context.index_path) as db:
            paths += [row['relative_path'] for row in db.execute(
                'SELECT relative_path FROM files_index WHERE source_id=? AND parent=? AND is_dir=0 AND available=1 LIMIT 4',
                (context.source.source_id,'' if parent=='.' else parent))]
    captures={}
    for path in paths:
        capture=metadata._preserved_file_capture(context.connection,context.source.source_id,path)
        if capture:captures[capture['item_id']]=capture
    if len(captures)!=1:raise ValueError('Select one retained YouTube save')
    item_id,capture=next(iter(captures.items()))
    record=capture['payload']['record']
    if record.get('layout_version',0) not in (0,1):raise ValueError('Saved bundle metadata is unavailable')
    observed=metadata.preserved_media(context.connection,record['id'],source_id=context.source.source_id,
        source_root=context.source.path,forbidden_roots=context.forbidden,hash_assets=False)
    assets=[];selected={}
    for role,facts in observed['assets'].items():
        if facts is None:continue
        asset_id=str(uuid5(NAMESPACE_URL,'urn:keivotos:youtube:asset:'+
                          json.dumps([item_id,role],separators=(',',':'))))
        output='source/youtube-owner.json' if role=='manifest' else facts['path']
        assets.append(portable.asset(output,asset_id=asset_id,role=role,size=facts['byte_size'],
            media_type='application/json' if role=='manifest' else 'text/plain' if role=='metadata' else None,required=role in ('media','manifest')))
        selected[output]=(Path(context.source.path),facts['path'],context.forbidden)
    library=item_catalog.library_for_legacy(
        context.connection,namespace='files.source',legacy_key=context.source.source_id)
    extension={'legacy':capture['payload']}
    if record.get('layout_version')==1:extension['legacy_manifest']='source/youtube-owner.json'
    if observed.get('details') is not None:extension['details']=observed['details']
    parents=[];relations=[]
    if len(capture['payload']['legacy_job_ids'])>1:
        parent_id=identity(capture['payload']['legacy_job_ids'][1])
        parents=[{'id':parent_id,'owner':'youtube','type':'saved-media'}]
        relations=[{'from':item_id,'to':parent_id,'kind':'retry-of','position':0}]
    value=portable.new(item_id=item_id,owner='youtube',item_type='saved-media',
        library_id=library['id'],library_name=context.source.display_name,storage='linked',assets=assets,
        origin={'url':record['url'],'captured_at':record.get('captured_at') or None},
        context={'parents':parents,'relations':relations},extensions={'youtube':extension})
    return value,portable.AssetRoots(selected)

def contribution():
    return portable.PortableOwner('youtube',describe,register)


def legacy_record(value,source_id,source_root,prefix,*,manifest_digest=None):
    from modules.youtube import metadata
    portable.validate(value)
    extension=value['extensions'].get('youtube',{})
    if set(extension)-{'details'} not in ({'legacy'},{'legacy','legacy_manifest'}):raise ValueError('Invalid legacy saved context')
    payload=extension['legacy']
    if not isinstance(payload,dict) or set(payload)!={'record','legacy_job_ids'}:
        raise ValueError('Missing legacy saved facts')
    record=dict(payload['record']);owners=payload['legacy_job_ids']
    if (value['item']!={'id':identity(record['id']),'owner':'youtube','type':'saved-media'}
            or record.get('layout_version',0) not in (0,1) or not isinstance(owners,list) or not owners
            or owners[0]!=record['id']):
        raise ValueError('Legacy saved identity changed')
    parts=record['path'].split('/')
    version=record.get('layout_version',0)
    if version==0:
        if len(parts)!=3 or parts[0]!='YouTube' or parts[1] not in owners:
            raise ValueError('Legacy saved file relationships changed')
    elif not isinstance(extension.get('legacy_manifest'),str):
        raise ValueError('Missing original saved manifest reference')
    expected={'media':record['path'],**{role:record[role] for role in ('thumbnail','avatar') if record.get(role)}}
    if version==1:expected['manifest']=extension['legacy_manifest']
    if 'details' in extension:
        from modules.youtube import details
        snapshot=details.validate(extension['details'],record)
        base=record.get('bundle_path','') if version==1 else Path(record['path']).parent.as_posix()
        if snapshot['description'] is not None:expected['metadata']=details.asset_path(snapshot,base)
    actual={a['role']:a['path'] for a in value['assets']}
    if (len(actual)!=len(value['assets']) or actual.get('media')!=record['path']
            or any(role not in expected or path!=expected[role] for role,path in actual.items())):
        raise ValueError('Legacy saved assets changed')
    for field in metadata.COLUMNS:
        record.setdefault(field,0 if field=='metadata_version' else None if field=='duration_seconds' else '')
    for field in ('path','thumbnail','avatar'):
        if record.get(field):record[field]='/'.join(p for p in (prefix,record[field]) if p)
    record.update(source_id=source_id,root=str(source_root),layout_version=2,bundle_path=prefix,
                  manifest_sha256=manifest_digest or hashlib.sha256(portable.encode(value)).hexdigest())
    fields=('id','url','source_id','root','video','audio','title','path','thumbnail','created_at',
            'retry_of',*metadata.COLUMNS,'layout_version','bundle_path','manifest_sha256')
    return {key:record[key] for key in fields if key in record},owners


def _register(context,record,owners):
    from modules.youtube import metadata
    from modules.youtube.user_schema import register_catalog_publication
    previous=metadata.preserved_capture(context.connection,record['id'])
    if previous is not None:
        old=previous['payload']['record']
        if (previous['payload']['legacy_job_ids']!=owners or
                any(old.get(key)!=value for key,value in record.items() if key!='root')):
            raise ValueError('Saved portable registration conflicts with retained capture facts')
        record=old
    return register_catalog_publication(context.connection,record,legacy_job_ids=owners,
        source_id=context.source.source_id,source_root=context.source.path,forbidden_roots=context.forbidden)

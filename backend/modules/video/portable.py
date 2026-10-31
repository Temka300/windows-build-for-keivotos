"""Video portable facts and exact source sidecars use the existing item engine."""
from pathlib import Path,PurePosixPath
from uuid import uuid5,NAMESPACE_URL
import copy
from services.item_manifest import PortableOwner,local_description,AssetRoots,stable_asset
from modules.video import metadata


def describe(context,selection,options):
    from files_base.serving import _INLINE_VIDEO
    from services import item_manifest as portable
    if context.resolve(selection).suffix.lower().lstrip('.') not in _INLINE_VIDEO:
        raise ValueError('Select a supported local video')
    source_records={};inputs={}
    for path,kind in metadata.scopes(selection,'video'):
        facts,observed,_,_=metadata.read_local(context,path,kind)
        if facts:source_records[metadata.key(context.source.source_id,path,kind)]=dict(facts=facts,revision='export')
        for record in observed:inputs[record['path']]=record
    value,root=local_description(context,selection,options,owner='video',item_type='video')
    # A complete native item remains byte/identity compatible without a new sidecar addition.
    external=[entry for entry in inputs.values() if entry['format']!='native']
    if not source_records or not external:return value,root
    value=copy.deepcopy(value);root=Path(root)
    authorized={a['path']:(root,a['path'],context.forbidden) for a in value['assets']}
    primary=context.resolve(selection).relative_to(root).as_posix()
    facts=metadata.effective(context.source.source_id,selection,'video',source_records,{})['fields']
    envelope=[]
    for (source_id,path,kind),record in source_records.items():
        try:relative=context.resolve(path).relative_to(root).as_posix()
        except ValueError:continue
        if relative!='.':envelope.append(dict(path=relative,kind=kind,facts=record['facts']))
    # Parent context outside an existing native item's root remains supplied facts on its selected video.
    envelope=[entry for entry in envelope if not(entry['path']==primary and entry['kind']=='video')]
    envelope.append(dict(path=primary,kind='video',facts=facts))
    extension=value['extensions'].setdefault('video',{});extension['metadata']=dict(version=1,subjects=envelope)
    used={a['path'] for a in value['assets']};positions={}
    for asset in value['assets']:positions[asset['role']]=max(positions.get(asset['role'],0),asset['position']+1)
    for entry in inputs.values():
        source=context.resolve(entry['path']);inside=False
        try:relative=source.relative_to(root).as_posix();inside=relative!=portable.MANIFEST
        except ValueError:relative=''
        if not inside:relative='metadata/video/'+entry['sha256']+'/'+source.name
        if relative in used:continue
        used.add(relative);role='metadata';position=positions.get(role,0);positions[role]=position+1
        asset=stable_asset(value['item']['id'],relative,role=role,position=position,size=source.stat().st_size,sha256=entry['sha256'])
        value['assets'].append(asset);authorized[relative]=(Path(context.source.path),entry['path'],context.forbidden)
    portable.validate(value)
    return value,AssetRoots(authorized)


def contribution():
    return PortableOwner('video',describe,metadata.import_manifest)

"""Export one original chapter/archive with its actual series path and pages."""
from pathlib import Path
from copy import deepcopy
from services.item_manifest import PortableOwner, local_description, AssetRoots, stable_asset
from modules.manga import metadata
from modules.manga import library

def describe(context,selection,options):
    target=context.resolve(selection)
    kind='folder' if target.is_dir() else 'archive'
    if kind=='archive' and target.suffix.lower().lstrip('.') not in library.ARCHIVES:
        raise ValueError('Select an original Manga chapter')
    facts=library.chapter_metadata(context.source,selection,kind,context.index_path,context.forbidden)
    paths=([(p,'page') for p in library.folder_pages(context.index_path,context.source.source_id,selection)]
           if kind=='folder' else [(selection,'archive')])
    if not paths:raise ValueError('Chapter has no original pages')
    value,root=local_description(context,selection,options,owner='manga',item_type='chapter',
        facts={'chapter':selection,'kind':kind,'page_count':facts['count'],
               'series_path':selection.split('/')[0] if '/' in selection else ''},paths=paths)
    series=next((item['path'] for item in library.catalog(context.index_path,[context.source]) if any(c['path']==selection and c['kind']==kind for c in item['chapters'])),None)
    if series is None:raise ValueError('Manga chapter is unavailable')
    fields,inputs,raws=metadata.read_local(context,series)
    if not any(entry['format']!='native' for entry in inputs):return value,root
    targets=metadata.preserve_raw(context,series,inputs,raws)
    metadata._fresh(context,series,'series',inputs)
    value=deepcopy(value);root=Path(root)
    value['extensions'].setdefault('manga',{})['metadata']=dict(version=1,fields=fields)
    authorized={asset['path']:(root,asset['path'],context.forbidden) for asset in value['assets']}
    used={asset['path']:asset for asset in value['assets']};position=max((a['position'] for a in value['assets'] if a['role']=='metadata'),default=-1)+1
    for entry,target in zip(inputs,targets):
        if entry['format']=='native':continue
        relative='metadata/manga/'+entry['sha256']+'/'+target.name
        if relative in used:
            if used[relative]['sha256']!=entry['sha256']:raise ValueError('Metadata export path collision')
            continue
        asset=stable_asset(value['item']['id'],relative,role='metadata',position=position,size=target.stat().st_size,sha256=entry['sha256']);position+=1
        value['assets'].append(asset);used[relative]=asset
        authorized[relative]=(target.parent,target.name,())
    from services.item_manifest import validate
    validate(value)
    return value,AssetRoots(authorized)

def contribution():
    return PortableOwner('manga',describe,metadata.import_manifest)

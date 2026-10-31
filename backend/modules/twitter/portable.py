"""Portable Twitter context references the preserved owner files, never duplicates a tweet."""
import hashlib
from pathlib import Path
from uuid import uuid5, NAMESPACE_URL
from services import item_manifest as portable
from modules.twitter import archive

def envelope(folder,spec,manifest,raw):
    from modules.twitter.user_schema import _catalog_key
    identity=str(uuid5(NAMESPACE_URL,'urn:keivotos:twitter:capture:'+
        _catalog_key(spec['source_id'],manifest['post_id'],manifest['capture_id'])))
    assets=[]
    for name,facts in manifest['files'].items():
        role=('source' if name=='source.json' else 'metadata' if name=='tweet.json'
              else 'avatar' if name in archive.AVATAR_LIMITS else 'video' if name=='video.mp4'
              else name.rsplit('.',1)[0])
        assets.append(portable.asset(name,role=role,asset_id=str(uuid5(NAMESPACE_URL,identity+':'+role)),
            size=facts['size'],sha256=facts['sha256'],
            media_type='application/json' if name.endswith('.json') else None))
    assets.append(portable.asset('manifest.json',role='manifest',
        asset_id=str(uuid5(NAMESPACE_URL,identity+':manifest')),size=len(raw),sha256=hashlib.sha256(raw).hexdigest(),
        media_type='application/json'))
    return portable.new(item_id=identity,owner='twitter',item_type='saved-post',
        library_id=spec.get('portable_library_id') or str(uuid5(NAMESPACE_URL,'urn:keivotos:source:'+spec['source_id'])),
        library_name='Twitter',assets=assets,origin={'url':manifest['source_url'],'captured_at':manifest['captured_at']},
        extensions={'twitter':{key:manifest[key] for key in ('post_id','capture_id','captured_at')}})

def adjacent(source,registered,suite_home,path):
    relative=(Path(path).parent/portable.MANIFEST).as_posix()
    value,_=portable.read(source.path,relative,forbidden=archive.forbidden_roots(source,registered,suite_home),check_assets=False)
    if value['item']['owner']!='twitter' or value['item']['type']!='saved-post':
        raise ValueError('Portable Twitter identity changed')
    facts=value['extensions'].get('twitter')
    if not isinstance(facts,dict) or set(facts)!={'post_id','capture_id','captured_at'}:
        raise ValueError('Missing portable Twitter context')
    archive._path_parts('captures/'+facts['post_id']+'/'+facts['capture_id']+'/manifest.json')
    manifests=[a for a in value['assets'] if a['role']=='manifest' and a['path']=='manifest.json']
    if len(manifests)!=1 or not manifests[0]['sha256']:raise ValueError('Missing owner manifest identity')
    return value,manifests[0]

def register(value,context,relative):
    from modules.twitter.user_schema import register_catalog_capture
    path=(Path(relative).parent.parent/'manifest.json').as_posix()
    return register_catalog_capture(context.connection,source_id=context.source.source_id,
        registered=context.registered,suite_home=context.suite_home,path=path)

def describe(context,selection,options):
    if options:raise ValueError('A Twitter capture already includes its source context')
    target=context.resolve(selection)
    if target.is_file():
        selection=target.parent.relative_to(context.source.path).as_posix()
        if selection=='.':selection=''
    path=(Path(selection)/portable.MANIFEST).as_posix()
    if context.resolve(path,must_exist=False).exists():
        value,_=portable.read(context.source.path,path,forbidden=context.forbidden)
        adjacent(context.source,context.registered,context.suite_home,(Path(selection)/'manifest.json').as_posix())
        return value,context.resolve(selection)
    owner_path=(Path(selection)/'manifest.json').as_posix()
    observed=archive.verified_assets(context.source,context.registered,context.suite_home,owner_path)
    raw=context.resolve(owner_path).read_bytes()
    import json
    value=envelope(context.resolve(selection),{'source_id':context.source.source_id,
                   'portable_library_id':portable.collection_identity(context.connection,context.source)},
                   json.loads(raw,object_pairs_hook=portable._unique),raw)
    return value,context.resolve(selection)

def contribution():
    return portable.PortableOwner('twitter',describe,register)

"""Small create-only preservation manifest; not a fresh-library importer."""
import hashlib
import json
import os
from pathlib import Path
import re

from modules.youtube import metadata, layout

from services import item_manifest as portable

MAX_BYTES=16384
READ_MAX_BYTES=portable.MAX_BYTES


def prepare(directory,job,result,facts):
    media=Path(result['path'])
    filename=('audio' if job['video']=='audio' else 'video')+media.suffix.lower()
    thumbnail=result.get('thumbnail','')
    cover='thumbnail'+Path(thumbnail).suffix.lower() if thumbnail else ''
    if cover and not re.fullmatch(r'thumbnail\.(jpg|jpeg|png|webp)',cover):
        raise ValueError('Unsupported saved thumbnail format.')
    for old,new in [(media.name,filename)]+([(thumbnail,cover)] if cover else []):
        if old==new:continue
        target=directory/new
        if any(p.name.casefold()==new.casefold() for p in directory.iterdir()):
            raise ValueError('Saved filename already exists; files were preserved.')
        (directory/old).rename(target)
    private=directory/'.keivotos'
    if private.is_symlink():raise ValueError('Saved metadata contains a symbolic link.')
    private.mkdir(exist_ok=True)
    value={'version':1,'saved_item_id':job['id'],'source_url':job['url'],
           'title':metadata.text(result['title']),'media':filename,'thumbnail':cover,'capture':facts}
    if not job.get('source_id'):
        # Compatibility for old callers without a collection identity. New
        # application acquisitions always supply their registered source.
        data=(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
        if len(data)>MAX_BYTES:raise ValueError('Saved metadata is too large.')
        with (private/'item.json').open('xb') as handle:
            handle.write(data);handle.flush();os.fsync(handle.fileno())
        return filename,cover,hashlib.sha256(data).hexdigest()
    from modules.youtube.portable import envelope
    value=envelope(directory,{**job,'portable_title':result['title']},value,snapshot=result.get('details'))
    digest=portable.write(directory,value)
    return filename,cover,digest


def read(root,job,owners,*,forbidden_roots=None,manifest_relative=None):
    def resolve(relative):
        if forbidden_roots is not None:
            from files_base import serving
            serving.resolve_within_source(root,relative,forbidden_roots,must_exist=False)
        return layout.resolve(root,relative)
    relative=job['bundle_path'];parts=relative.split('/')
    folder=resolve(relative) if relative else Path(root)
    if not folder.exists() and not job['path']:return None
    manifest=resolve(manifest_relative or '/'.join(p for p in (relative,'.keivotos/item.json') if p))
    if forbidden_roots is not None and not manifest.is_file():
        raise ValueError('Saved metadata is unavailable.')
    with manifest.open('rb') as handle:data=handle.read(READ_MAX_BYTES+1)
    if len(data)>READ_MAX_BYTES or hashlib.sha256(data).hexdigest()!=job['manifest_sha256']:
        raise ValueError('Saved metadata changed; files were preserved.')
    value=json.loads(data,object_pairs_hook=portable._unique)
    generic=isinstance(value,dict) and value.get('format')==portable.FORMAT
    if generic:
        from modules.youtube.portable import saved
        value=saved(portable.decode(data))
        if manifest_relative is None:
            portable.read(root,'/'.join(p for p in (relative,portable.MANIFEST) if p),
                          forbidden=forbidden_roots or (),check_assets=False)
            for declared in portable.decode(data)['assets']:
                if declared['role']!='metadata':portable.observe(folder,declared,forbidden_roots or ())
        else:
            for declared in portable.decode(data)['assets']:
                if declared['role']!='metadata':portable.observe(folder,declared,forbidden_roots or (),hash_bytes=False)
    elif len(data)>MAX_BYTES or len(parts)!=3 or parts[0]!='YouTube':
        raise ValueError('Saved bundle location changed.')
    if (not isinstance(value,dict) or set(value)!={'version','saved_item_id','source_url','title','media','thumbnail','capture'}
            or type(value['version']) is not int or value['version']!=1 or value['saved_item_id'] not in owners
            or value['source_url']!=job['url'] or not generic and not parts[2].endswith(' ['+value['saved_item_id'][:16]+']')):
        raise ValueError('Saved bundle identity changed.')
    media=value['media'];cover=value['thumbnail']
    if (not isinstance(media,str) or not re.fullmatch(r'(video|audio)\.[a-z0-9]{1,12}',media)
            or not isinstance(cover,str) or cover and not re.fullmatch(r'thumbnail\.(jpg|jpeg|png|webp)',cover)
            or not isinstance(value['title'],str) or metadata.text(value['title'])!=value['title']):
        raise ValueError('Saved bundle relationships changed.')
    facts=metadata.validate(value['capture'],job['url'],media)
    for name,expected in facts.items():
        if name=='avatar' and expected:expected='/'.join(p for p in (relative,expected) if p)
        if job[name]!=expected:raise ValueError('Captured metadata changed.')
    path='/'.join(p for p in (relative,media) if p)
    if job['path'] and job['path']!=path:raise ValueError('Saved output location changed.')
    target=resolve(path)
    if not target.is_file() or not target.stat().st_size:raise ValueError('Published output is unavailable or empty.')
    thumbnail='/'.join(p for p in (relative,cover) if p) if cover else ''
    if job['thumbnail'] and job['thumbnail']!=thumbnail:raise ValueError('Saved cover location changed.')
    if thumbnail and not resolve(thumbnail).is_file():thumbnail=''
    return dict(path=path,thumbnail=thumbnail,title=job['title'] or value['title'])

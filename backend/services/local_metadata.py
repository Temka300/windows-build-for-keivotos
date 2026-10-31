"""Bounded local-format adapters. Bytes in, supplied facts out; no file/network IO."""
import json
from defusedxml.ElementTree import fromstring

MAX_BYTES=192*1024
FIELDS=('summary','tags')
LISTS={'tags'}
LIMITS={'summary':16384}


class MetadataError(ValueError):
    pass


def fields(value,*,personal=False):
    if not isinstance(value,dict) or set(value)-set(FIELDS):raise MetadataError('Unsupported detail fields.')
    result={}
    for key,item in value.items():
        if item is None:
            if personal:result[key]=None
            continue
        if key in LISTS:
            if not isinstance(item,list) or len(item)>64:raise MetadataError('Too many detail values.')
            accepted=[]
            for word in item:
                if not isinstance(word,str) or len(word)>1024 or '\0' in word:raise MetadataError('Invalid detail value.')
                try:word.encode('utf-8')
                except UnicodeError as error:raise MetadataError('Invalid detail text.') from error
                word=word.strip()
                if word and word not in accepted:accepted.append(word)
            if accepted or personal:result[key]=accepted
        else:
            if not isinstance(item,str) or len(item)>LIMITS[key] or '\0' in item:raise MetadataError('Invalid '+key+'.')
            try:item.encode('utf-8')
            except UnicodeError as error:raise MetadataError('Invalid detail text.') from error
            item=item.strip()
            if item or personal:result[key]=item
    if len(json.dumps(result,ensure_ascii=False).encode('utf-8'))>128*1024:raise MetadataError('Detail fields exceed the size limit.')
    return result


def _bounded(raw):
    if not isinstance(raw,bytes) or len(raw)>MAX_BYTES:raise MetadataError('Local details exceed the size limit.')


def _unique(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise MetadataError('Duplicate metadata key.')
        value[key]=item
    return value


def _constant(_):raise MetadataError('Non-finite metadata value.')


def details_json(raw):
    _bounded(raw)
    try:value=json.loads(raw,object_pairs_hook=_unique,parse_constant=_constant)
    except (ValueError,UnicodeError,RecursionError) as error:raise MetadataError('Could not read details.json.') from error
    if not isinstance(value,dict):raise MetadataError('Expected a details object.')
    # These are Suwayomi's documented fields. Unknown input stays in the original asset.
    return fields({key:value[old] for old,key in [('description','summary'),('genre','tags')] if old in value})


def nfo(raw):
    _bounded(raw)
    try:root=fromstring(raw,forbid_dtd=True,forbid_entities=True,forbid_external=True)
    except Exception as error:raise MetadataError('Could not read video NFO.') from error
    name=lambda node:node.tag.rsplit('}',1)[-1].lower() if isinstance(node.tag,str) else ''
    if name(root) not in ('tvshow','season','episodedetails','movie','musicvideo'):raise MetadataError('Unsupported video NFO root.')
    count=0
    def bound(node,depth=0):
        nonlocal count
        count+=1
        if depth>64 or count>8192:raise MetadataError('Video NFO is too complex.')
        for child in node:bound(child,depth+1)
    bound(root)
    data={};tags=[]
    for node in root:
        tag=name(node);text=''.join(node.itertext()).strip()
        if not text:continue
        if tag in ('plot','review'):data['summary']=text
        elif tag in ('tag','genre'):tags.append(text)
    if tags:data['tags']=tags
    # Other supplied facts stay only in the preserved source bytes.
    return fields(data)


def comic_info(raw):
    """Suwayomi-compatible ComicInfo facts; unsupported fields remain raw bytes."""
    _bounded(raw)
    try:root=fromstring(raw,forbid_dtd=True,forbid_entities=True,forbid_external=True)
    except Exception as error:raise MetadataError('Could not read ComicInfo.xml.') from error
    name=lambda node:node.tag.rsplit('}',1)[-1] if isinstance(node.tag,str) else ''
    if name(root)!='ComicInfo':raise MetadataError('Unsupported ComicInfo root.')
    count=0
    def bound(node,depth=0):
        nonlocal count
        count+=1
        if depth>64 or count>8192:raise MetadataError('ComicInfo is too complex.')
        for child in node:bound(child,depth+1)
    bound(root)
    result={};tags=[];seen=set()
    for node in root:
        tag=name(node)
        if tag not in ('Summary','Genre','Tags','CategoriesTachiyomi'):continue
        if tag in seen:raise MetadataError('Duplicate ComicInfo field.')
        seen.add(tag);text=''.join(node.itertext()).strip()
        if tag=='Summary':result['summary']=text
        else:tags.extend(word.strip() for word in text.split(',') if word.strip())
    if tags:result['tags']=tags
    return fields(result)

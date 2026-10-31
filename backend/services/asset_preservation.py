"""Referenced durable files, verified snapshots and create-only recovery.

Generated previews and media libraries are never inferred to be owned here.
Owners declare actual referenced originals; missing files remain explicit.
"""
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from services import item_manifest
from storage_layout import portable_path,portable_relative


@dataclass(frozen=True)
class Reference:
    owner: str
    key: str
    source: Path
    algorithm: str = ''
    digest: str = ''
    size: int | None = None
    permitted: bool = True


SCHEMA="""CREATE TABLE IF NOT EXISTS kv_preserved_assets(
 owner TEXT NOT NULL,reference_key TEXT NOT NULL,sha256 TEXT NOT NULL,
 relative_path TEXT NOT NULL,original_path TEXT NOT NULL,size INTEGER NOT NULL,
 PRIMARY KEY(owner,reference_key,sha256))"""


def _table(db,name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone() is not None


def declarations(db,registry,configuration,include_owners=None):
    for descriptor in registry:
        if descriptor.transfer_provider is None or include_owners is not None and descriptor.slug not in include_owners:continue
        contribution=descriptor.transfer_provider()
        if contribution.reference_files is not None:
            yield from contribution.reference_files(db,configuration)


def _observed(reference):
    path=Path(reference.source).absolute()
    if path.resolve()!=path:raise ValueError('Referenced file contains links')
    info=item_manifest.asset(path.name,asset_id='reference',size=reference.size)
    result=item_manifest.observe(path.parent,info)
    if reference.algorithm:
        if reference.algorithm not in ('md5','sha256'):raise ValueError('Unsupported referenced file hash')
        with path.open('rb') as stream:digest=hashlib.file_digest(stream,reference.algorithm).hexdigest()
        if digest!=reference.digest:raise ValueError('Referenced original differs from its captured hash')
        if item_manifest.observe(path.parent,info,hash_bytes=False)['identity']!=result['identity']:
            raise ValueError('Referenced file changed while verifying')
    return result


def preserve(db,home,registry,configuration,*,include_owners=None,include_references=None):
    """Copy known originals into canonical assets; never rewrite legacy rows."""
    if db.in_transaction:raise ValueError('Preserve referenced bytes outside pending writes')
    db.execute(SCHEMA);db.commit()
    available=[];missing=[]
    for reference in declarations(db,registry,configuration,include_owners):
        if include_owners is not None and reference.owner not in include_owners:continue
        if not isinstance(reference,Reference):raise ValueError('Invalid owner file declaration')
        if include_references is not None and (reference.owner,reference.key) not in include_references:continue
        original_source=reference.source
        if not reference.permitted or not Path(reference.source).is_file():
            fallback=resolve(db,home,reference.owner,reference.key,expected_source=reference.source)
            if fallback is None:
                missing.append(dict(owner=reference.owner,key=reference.key));continue
            reference=Reference(reference.owner,reference.key,fallback,reference.algorithm,reference.digest,reference.size)
        observation=_observed(reference)
        suffix=Path(reference.source).suffix.lower()
        if suffix and (len(suffix)>16 or not suffix[1:].isalnum()):suffix=''
        relative='assets/'+portable_relative(reference.owner)+'/originals/'+observation['sha256']+suffix
        target=portable_path(home,relative,(),must_exist=False)
        target.parent.mkdir(parents=True,exist_ok=True)
        item_manifest.copy_observation(observation,target)
        with db:
            db.execute('INSERT OR IGNORE INTO kv_preserved_assets VALUES(?,?,?,?,?,?)',
                (reference.owner,reference.key,observation['sha256'],relative,str(original_source),observation['size']))
        available.append(dict(owner=reference.owner,key=reference.key,path=relative,
                              sha256=observation['sha256'],size=observation['size']))
    return dict(files=available,missing=missing)


def resolve(db,home,owner,key,*,expected_source=None):
    """Preserved metadata is provenance, never an arbitrary absolute read path."""
    if not _table(db,'kv_preserved_assets'):return None
    cursor=db.cursor();cursor.row_factory=sqlite3.Row
    try:
        rows=cursor.execute('SELECT * FROM kv_preserved_assets WHERE owner=? AND reference_key=? ORDER BY rowid DESC',
                            (owner,key)).fetchall()
    finally:cursor.close()
    for row in rows:
        if expected_source is not None and not _same_origin(row['original_path'],str(expected_source)):continue
        relative=row['relative_path']
        if not relative.startswith('assets/'+portable_relative(owner)+'/originals/'):
            raise ValueError('Preserved asset location changed')
        path=portable_path(home,relative,(),must_exist=False)
        if path.is_file():
            item_manifest.observe(home,item_manifest.asset(relative,asset_id='preserved',
                size=row['size'],sha256=row['sha256']),hash_bytes=False)
            return path
    return None


def snapshot(database,home,registry,configuration,destination,*,include_owners=None,include_references=None):
    """Capture references against the exact SQLite snapshot supplied by backup."""
    with closing(sqlite3.connect(database)) as db:
        db.row_factory=sqlite3.Row
        result=preserve(db,home,registry,configuration,include_owners=include_owners,include_references=include_references)
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
    for entry in result['files']:
        info=item_manifest.asset(entry['path'],asset_id='snapshot',size=entry['size'],sha256=entry['sha256'])
        observation=item_manifest.observe(home,info)
        target=portable_path(destination,entry['path'],(),must_exist=False)
        target.parent.mkdir(parents=True,exist_ok=True)
        item_manifest.copy_observation(observation,target)
    item_manifest._create(destination/'references.json',item_manifest.encode(dict(format=1,**result)))
    return result


def validate_snapshot(directory):
    directory=Path(directory)
    manifest=portable_path(directory,'references.json')
    if manifest.stat().st_size>128*1024*1024:raise ValueError('Referenced-file snapshot exceeds its limit')
    value=json.loads(manifest.read_bytes(),object_pairs_hook=item_manifest._unique)
    if set(value)!={'format','files','missing'} or value['format']!=1:raise ValueError('Unsupported referenced-file snapshot')
    for entry in value['files']:
        owner=portable_relative(entry['owner'])
        if not entry['path'].startswith('assets/'+owner+'/originals/'):
            raise ValueError('Invalid preserved snapshot path')
        item_manifest.observe(directory,item_manifest.asset(entry['path'],asset_id='restore',
            size=entry['size'],sha256=entry['sha256']))
    return value


def restore_snapshot(directory,home):
    """Publish verified bytes create-only before acknowledging restored records."""
    value=validate_snapshot(directory)
    for entry in value['files']:
        info=item_manifest.asset(entry['path'],asset_id='restore',size=entry['size'],sha256=entry['sha256'])
        observation=item_manifest.observe(directory,info)
        target=portable_path(home,entry['path'],(),must_exist=False);target.parent.mkdir(parents=True,exist_ok=True)
        item_manifest.copy_observation(observation,target)
    return dict(restored=len(value['files']),missing=len(value['missing']))


def _same_origin(left,right):
    from pathlib import PureWindowsPath
    if PureWindowsPath(left).drive and PureWindowsPath(right).drive:
        return PureWindowsPath(left)==PureWindowsPath(right)
    return left==right

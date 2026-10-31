"""Portable metadata and create-only copies on isolated linked libraries."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from files_base import sources, index, serving
from module_registry import build_registry
from services import item_catalog
from services import item_manifest as portable

@pytest.fixture
def local(tmp_path):
    root=tmp_path/'library';root.mkdir()
    db=sqlite3.connect(tmp_path/'user.sqlite');db.row_factory=sqlite3.Row
    sources.ensure_sources_schema(db);item_catalog.ensure_catalog_schema(db)
    source=sources.register_source(db,root,'Publisher')
    idx=tmp_path/'index.sqlite'
    with index.open_index(idx) as connection:index.scan_source(connection,source.source_id,root)
    context=portable.Context(source,(source,),tmp_path/'home',idx,db,build_registry(tmp_path/'home','test'))
    yield context
    db.close()

def manifest(root,*,owner='files'):
    (root/'original.bin').write_bytes(b'original')
    return portable.new(item_id='stable-item',owner=owner,item_type='file',library_id='stable-library',
        assets=[portable.asset('original.bin',asset_id='stable-asset',size=8,
            sha256=hashlib.sha256(b'original').hexdigest())],origin={'unknown_date':None})

def tree(root):
    return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}

def test_roundtrip_register_without_jobs_and_preserve_caller_transaction(local):
    root=Path(local.source.path);value=manifest(root)
    portable.write(root,value)
    local.connection.execute('CREATE TABLE personal(note TEXT)')
    local.connection.execute("INSERT INTO personal VALUES('pending')")
    assert portable.import_item(local)=='stable-item'
    assert local.connection.in_transaction
    assert portable.import_item(local)=='stable-item'
    assert item_catalog.item_for_legacy(local.connection,namespace='portable.identity',legacy_key='stable-item')['id']=='stable-item'
    assert not local.connection.execute("SELECT 1 FROM sqlite_master WHERE name='kv_operations'").fetchone()
    local.connection.rollback()
    assert not local.connection.execute('SELECT 1 FROM kv_items').fetchone()
    assert portable.read(root,hash_assets=True)[0]==value

@pytest.mark.parametrize('path',['../escape','/absolute','C:/device','C:relative','//server/file',
    'bad\\path','a/./b','a//b','a/../b','file.','file ','CON','NUL.txt','COM1','a/aux.png',
    '.keivotos/item.json','.keivotos/history/original.json'])
def test_reject_paths(local,path):
    value=manifest(Path(local.source.path))
    value['assets'][0]['path']=path
    with pytest.raises(ValueError):portable.validate(value)

@pytest.mark.parametrize('change',[
    lambda v:v.update(version=2),lambda v:v.update(version=True),
    lambda v:v.update(unrecognized=True),lambda v:v['item'].update(owner='../../optional'),
    lambda v:v['assets'][0].update(size=-1),lambda v:v['assets'][0].update(sha256='bad'),
    lambda v:v['assets'][0].update(required=1),lambda v:v.update(context={'relations':[{'from':'missing','to':'stable-item','kind':'contains','position':0}]}),
])
def test_reject_malformed(local,change):
    value=manifest(Path(local.source.path));change(value)
    with pytest.raises(ValueError):portable.validate(value)

def test_duplicate_json_keys_nonfinite_limits_and_cycles(local):
    value=manifest(Path(local.source.path))
    for raw in (b'{"a":1,"a":2}',b'{"a":NaN}',b'\xff',b' '* (portable.MAX_BYTES+1)):
        with pytest.raises(ValueError):portable.decode(raw)
    value['context']={'parents':[{'id':'parent','owner':'files','type':'folder'}],
        'relations':[{'from':'parent','to':'stable-item','kind':'contains','position':0},
                     {'from':'stable-item','to':'parent','kind':'contains','position':0}]}
    with pytest.raises(ValueError,match='Cyclic'):portable.validate(value)

def test_conflicting_asset_identity_and_case_alias(local):
    value=manifest(Path(local.source.path))
    other={**value['assets'][0],'position':1,'path':'ORIGINAL.bin'}
    value['assets'].append(other)
    with pytest.raises(ValueError,match='collide'):portable.validate(value)
    other['path']='another.bin'
    with pytest.raises(ValueError,match='conflicting'):portable.validate(value)

def test_write_exact_retry_conflict_refresh_history_and_interruption(local,monkeypatch):
    root=Path(local.source.path);value=manifest(root)
    digest=portable.write(root,value);original=(root/portable.MANIFEST).read_bytes()
    assert portable.write(root,value)==digest
    changed={**value,'origin':{'title':'new captured revision'}}
    with pytest.raises(ValueError,match='conflict'):portable.write(root,changed)
    import os
    replace_file=os.replace
    def fail(*args):raise OSError('interrupted')
    monkeypatch.setattr(os,'replace',fail)
    with pytest.raises(OSError):portable.write(root,changed,refresh=True)
    assert (root/portable.MANIFEST).read_bytes()==original
    assert (root/'.keivotos/history'/ (digest+'.json')).read_bytes()==original
    monkeypatch.setattr(os,'replace',replace_file)
    portable.write(root,changed,refresh=True)
    assert portable.read(root)[0]==changed
    assert (root/'original.bin').read_bytes()==b'original'

def test_linked_preservation_and_export_never_write_originals(local,tmp_path):
    root=Path(local.source.path);(root/'song.wav').write_bytes(b'publisher')
    before=tree(root)
    value,_=portable.local_description(local,'song.wav',{},owner='files',item_type='file')
    portable.preserve(local.suite_home,value)
    destination=tmp_path/'copy'
    portable.export_item(local,'song.wav',destination,request_id='same-request')
    assert portable.export_item(local,'song.wav',destination,request_id='same-request')==destination
    assert tree(root)==before
    copied=portable.read(destination,hash_assets=True)[0]
    assert copied['item']['id']==value['item']['id']
    assert copied['assets'][0]['sha256']==hashlib.sha256(b'publisher').hexdigest()
    assert copied['origin']=={}
    assert (destination/'song.wav').read_bytes()==b'publisher'

def test_export_conflicts_and_source_changes_are_retained(local,tmp_path):
    root=Path(local.source.path);(root/'file.bin').write_bytes(b'first')
    target=tmp_path/'copy'
    portable.export_item(local,'file.bin',target,request_id='request')
    (root/'file.bin').write_bytes(b'other')
    with pytest.raises(ValueError,match='differs'):
        portable.export_item(local,'file.bin',target,request_id='request')
    assert (target/'file.bin').read_bytes()==b'first'
    with pytest.raises(ValueError):portable.export_item(local,'file.bin',root/'inside',request_id='second')
    with pytest.raises(ValueError):portable.export_item(local,'file.bin',local.suite_home/'copy',request_id='second')

def test_known_bytes_optional_assets_and_passive_no_media_hash(local,monkeypatch):
    root=Path(local.source.path);value=manifest(root)
    value['assets'].append(portable.asset('missing.jpg',role='cover',required=False))
    portable.write(root,value)
    def forbidden(*args):raise AssertionError('passive read hashed media')
    monkeypatch.setattr(hashlib,'file_digest',forbidden)
    assert portable.read(root)[1][1] is None
    (root/'original.bin').write_bytes(b'tampered')
    assert portable.read(root)[0]==value
    monkeypatch.undo()
    with pytest.raises(ValueError,match='hash'):portable.read(root,hash_assets=True)

def test_hidden_nested_private_and_symlink_guards(local,tmp_path):
    root=Path(local.source.path);manifest(root)
    with pytest.raises(ValueError):replace(local,source=replace(local.source,visible=False)).resolve('original.bin')
    child=sources.register_source(local.connection,root/'child','Nested');Path(child.path).mkdir()
    (Path(child.path)/'private.bin').write_bytes(b'child')
    nested=replace(local,registered=(local.source,child))
    with pytest.raises(serving.ServeDenied):nested.resolve('child/private.bin')
    if sys.platform!='win32':
        (root/'linked.bin').symlink_to(root/'original.bin')
        with pytest.raises(serving.ServeDenied):local.resolve('linked.bin')

def test_import_owner_absence_conflict_and_discovery(local):
    root=Path(local.source.path);value=manifest(root);portable.write(root,value)
    result=portable.discover(local)
    assert result['imported']==['stable-item'] and not result['refused']
    assert portable.discover(local)==result
    changed={**value,'item':{**value['item'],'owner':'absent'}}
    with pytest.raises(ValueError,match='not installed'):portable.register(changed,local,portable.MANIFEST)
    wrong={**value,'item':{**value['item'],'type':'conflict'}}
    with pytest.raises(ValueError):portable.register(wrong,local,portable.MANIFEST)
    assert item_catalog.item_for_legacy(local.connection,namespace='portable.identity',legacy_key='stable-item')['item_type']=='file'


def test_interrupted_asset_copy_retries_without_replacing_evidence(local,tmp_path,monkeypatch):
    root=Path(local.source.path);(root/'large.bin').write_bytes(b'generated original')
    destination=tmp_path/'export'
    real_link=portable.os.link
    interrupted=[]
    def fail_once(source,target):
        if Path(target).name=='large.bin' and not interrupted:
            interrupted.append(Path(source))
            raise OSError('copy publication interrupted')
        return real_link(source,target)
    monkeypatch.setattr(portable.os,'link',fail_once)
    with pytest.raises(OSError,match='publication interrupted'):
        portable.export_item(local,'large.bin',destination,request_id='retry')
    partial=interrupted[0]
    assert partial.is_file() and not destination.exists()
    assert portable.export_item(local,'large.bin',destination,request_id='retry')==destination
    assert partial.read_bytes()==b'generated original'
    assert (destination/'large.bin').read_bytes()==(root/'large.bin').read_bytes()

def test_completed_assets_recover_after_lost_manifest_ack(local,tmp_path,monkeypatch):
    root=Path(local.source.path);(root/'original.bin').write_bytes(b'original bytes')
    destination=tmp_path/'export';real_write=portable.write
    def fail(root,value,**options):
        if Path(root)==destination:raise OSError('metadata acknowledgement interrupted')
        return real_write(root,value,**options)
    monkeypatch.setattr(portable,'write',fail)
    with pytest.raises(OSError,match='acknowledgement'):
        portable.export_item(local,'original.bin',destination,request_id='retry')
    assert (destination/'original.bin').read_bytes()==b'original bytes'
    monkeypatch.setattr(portable,'write',real_write)
    portable.export_item(local,'original.bin',destination,request_id='retry')
    assert portable.read(destination,hash_assets=True)

@pytest.mark.parametrize('newline',['\n','\r\n'])
def test_readonly_authored_files_keep_exact_bytes_and_metadata(local,tmp_path,newline):
    root=Path(local.source.path);source=root/'authored.md'
    raw=('# Original'+newline+'한글  '+newline).encode()
    source.write_bytes(raw);source.chmod(0o444)
    try:
        before=source.stat()
        copied=portable.export_item(local,'authored.md',tmp_path/'copy',request_id='read-only')
        assert source.read_bytes()==(copied/'authored.md').read_bytes()==raw
        assert source.stat().st_mtime_ns==before.st_mtime_ns
        assert source.stat().st_mode==before.st_mode
    finally:source.chmod(0o644)

def test_bounded_refresh_and_metadata_symlink_refuse_before_writes(local,tmp_path):
    root=Path(local.source.path);value=manifest(root);portable.write(root,value)
    marker=root/portable.MANIFEST;marker.write_bytes(b'x'*(portable.MAX_BYTES+1))
    before=tree(root)
    with pytest.raises(ValueError,match='limit'):portable.write(root,value,refresh=True)
    assert tree(root)==before
    if sys.platform!='win32':
        marker.unlink();outside=tmp_path/'outside.json';outside.write_bytes(portable.encode(value))
        marker.symlink_to(outside)
        with pytest.raises(serving.ServeDenied):portable.write(root,value,refresh=True)
        assert outside.read_bytes()==portable.encode(value)


def test_import_preserves_exact_formatted_metadata_and_refresh_location(local):
    root=Path(local.source.path);value=manifest(root);portable.write(root,value)
    raw=(json.dumps(value,ensure_ascii=False,indent=4)+'\n').encode()
    (root/portable.MANIFEST).write_bytes(raw)
    portable.import_item(local)
    captured=local.suite_home/'preserved'/value['library']['id']/value['item']['id']/'captures'
    assert (captured/(hashlib.sha256(raw).hexdigest()+'.json')).read_bytes()==raw
    changed={**value,'origin':{'source_revision':'explicit replacement'}}
    portable.write(root,changed,refresh=True);portable.import_item(local)
    bindings=portable.bindings(local.connection,source_id=local.source.source_id)
    assert len(bindings)==1 and bindings[0]['digest']==hashlib.sha256(portable.encode(changed)).hexdigest()
    assert local.connection.execute("SELECT count(*) FROM kv_item_captures WHERE namespace='portable.location'").fetchone()[0]==2


def test_new_collection_matches_existing_source_preservation_bridge(local):
    from services.job_store_migration import preserve_sources
    assert portable.collection_identity(local.connection,local.source)==local.source.source_id
    preserve_sources(local.connection)
    assert portable.collection_identity(local.connection,local.source)==local.source.source_id
    local.connection.commit()

def test_explicit_rescan_never_promotes_acquisition_staging(local):
    root=Path(local.source.path)
    stage=root/'.keivotos/youtube/attempt';stage.mkdir(parents=True)
    value=manifest(stage);portable.write(stage,value)
    before=tree(root)
    assert portable.discover(local)=={'imported':[],'refused':[]}
    with pytest.raises(ValueError,match='staging'):
        portable.import_item(local,'.keivotos/youtube/attempt/'+portable.MANIFEST)
    assert tree(root)==before
    assert not local.connection.execute('SELECT 1 FROM kv_items').fetchone()

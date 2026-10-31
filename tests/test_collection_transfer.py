"""Selected folder export retains original structure and bounded metadata."""
from contextlib import closing
from pathlib import Path
import hashlib,json,sqlite3,sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from files_base import sources,index,annotations
from module_registry import build_registry
from services import item_manifest as portable,item_catalog,collection_transfer as transfer


@pytest.fixture
def collection(tmp_path):
    root=tmp_path/'Publisher album';disc=root/'Disc 2';disc.mkdir(parents=True)
    (disc/'07 Original 한글.flac').write_bytes(b'original audio')
    (root/'cover.jpg').write_bytes(b'original cover');(root/'publisher.cue').write_bytes(b'original cue\r\n')
    home=tmp_path/'home';home.mkdir()
    db=sqlite3.connect(home/'essential.sqlite');db.row_factory=sqlite3.Row
    sources.ensure_sources_schema(db);item_catalog.ensure_catalog_schema(db)
    source=sources.register_source(db,root,'Publisher album',role='files')
    annotations.ensure_annotations_schema(db)
    selected=annotations.upsert_annotation(db,subject_kind='file',content_hash=hashlib.md5(b'original audio').hexdigest(),
        source_id=source.source_id,relative_path='Disc 2/07 Original 한글.flac',description='My personal note')
    unrelated=annotations.upsert_annotation(db,subject_kind='file',content_hash='a'*32,
        source_id='unrelated',relative_path='elsewhere',description='Never export this')
    with index.open_index(home/'files.sqlite') as idx:index.scan_source(idx,source.source_id,root)
    context=portable.Context(source,(source,),home,home/'files.sqlite',db,build_registry(home,'test'))
    yield context,selected.id,unrelated.id
    db.close()


def test_export_collection_keeps_publisher_tree_original_bytes_and_exact_retry(collection,tmp_path):
    context,_,_=collection;before={p.relative_to(context.source.path).as_posix():p.read_bytes()
        for p in Path(context.source.path).rglob('*') if p.is_file()}
    output=tmp_path/'portable'
    first=transfer.export_collection([context],output,request_id='exact')
    assert transfer.export_collection([context],output,request_id='exact')==first
    copied=output/'roots'/context.source.source_id
    assert {p.relative_to(copied).as_posix():p.read_bytes() for p in copied.rglob('*') if p.is_file()}==before
    assert transfer.inspect_collection(output,verify_files=True)['files']==3
    assert {p.relative_to(context.source.path).as_posix():p.read_bytes() for p in Path(context.source.path).rglob('*') if p.is_file()}==before


@pytest.mark.parametrize('personal',[False,True])
def test_personal_records_are_opt_in_and_do_not_include_unrelated_notes(collection,tmp_path,personal):
    context,selected,unrelated=collection
    context.connection.execute('BEGIN IMMEDIATE')
    annotations.import_catalog_records(context.connection)
    context.connection.commit()
    output=tmp_path/'packet';transfer.export_collection([context],output,request_id='notes',include_personal=personal)
    with closing(sqlite3.connect(output/'collection.sqlite')) as db:
        payload=[r[0] for r in db.execute("SELECT row_json FROM records WHERE kind IN ('personal','catalog')")]
    text=' '.join(payload)
    assert ('My personal note' in text)==personal
    assert 'Never export this' not in text


def test_existing_output_refuses_without_replacing_any_file(collection,tmp_path):
    context,_,_=collection;output=tmp_path/'existing';output.mkdir();(output/'mine').write_bytes(b'preserve')
    with pytest.raises(ValueError,match='receipt'):
        transfer.export_collection([context],output,request_id='refused')
    assert (output/'mine').read_bytes()==b'preserve'


def test_packet_byte_tampering_is_refused(collection,tmp_path):
    context,_,_=collection;output=tmp_path/'packet'
    transfer.export_collection([context],output,request_id='tamper')
    target=output/'roots'/context.source.source_id/'cover.jpg';target.write_bytes(b'changed')
    with pytest.raises(ValueError):transfer.inspect_collection(output,verify_files=True)


def test_hidden_or_nested_sources_cannot_leak_into_parent_export(collection,tmp_path):
    context,_,_=collection
    hidden=portable.Context(__import__('dataclasses').replace(context.source,visible=False),context.registered,
        context.suite_home,context.index_path,context.connection,context.registry)
    with pytest.raises(ValueError):transfer.export_collection([hidden],tmp_path/'refused',request_id='hidden')
    child=Path(context.source.path)/'private';child.mkdir();(child/'secret.txt').write_bytes(b'not parent content')
    registered=sources.register_source(context.connection,child,'Child')
    guarded=portable.Context(context.source,(context.source,registered),context.suite_home,context.index_path,context.connection,context.registry)
    output=tmp_path/'parent';transfer.export_collection([guarded],output,request_id='parent')
    assert not (output/'roots'/context.source.source_id/'private/secret.txt').exists()


def test_current_child_fence_also_excludes_stale_parent_catalog_and_personal_records(collection,tmp_path):
    context,_,_=collection
    child=Path(context.source.path)/'private';child.mkdir();(child/'secret.txt').write_bytes(b'secret')
    registered=sources.register_source(context.connection,child,'Private child')
    item=item_catalog.ensure_item(context.connection,namespace='youtube.download',legacy_key='private',
        owner='youtube',item_type='saved-media')
    item_catalog.record_capture(context.connection,item_id=item,namespace='youtube.download',capture_key='private',
        format_version=1,payload={'record':{'source_id':context.source.source_id,'path':'private/secret.txt',
                                           'title':'Private source fact must stay private'}})
    annotations.upsert_annotation(context.connection,subject_kind='dir',source_id=context.source.source_id,
                                  relative_path='private',description='Private parent note')
    guarded=portable.Context(context.source,tuple(sources.list_sources(context.connection)),context.suite_home,
                             context.index_path,context.connection,context.registry)
    output=tmp_path/'parent'
    transfer.export_collection([guarded],output,request_id='private-child',include_personal=True)
    with closing(sqlite3.connect(output/'collection.sqlite')) as db:
        payload=' '.join(r[0] for r in db.execute('SELECT row_json FROM records'))
    assert 'Private source fact must stay private' not in payload and 'Private parent note' not in payload
    assert not (output/'roots'/context.source.source_id/'private').exists()

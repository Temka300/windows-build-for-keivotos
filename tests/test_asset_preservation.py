"""Required original bytes survive job loss, missing drives and snapshot copies."""
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import hashlib
import shutil
import sqlite3
import sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from files_base import annotations,attachment_store
from module_registry import ModuleRegistry
from modules.files import descriptor
from services import asset_preservation as assets


@pytest.fixture
def case(tmp_path):
    home=tmp_path/'home';home.mkdir()
    original=tmp_path/'original store'
    data=b'authored attachment original';digest=hashlib.md5(data).hexdigest()
    path=attachment_store.attachment_path(original,digest,'png');path.parent.mkdir(parents=True);path.write_bytes(data)
    database=home/'essential.sqlite'
    with closing(sqlite3.connect(database)) as db:
        db.row_factory=sqlite3.Row;annotations.ensure_annotations_schema(db)
        db.execute("INSERT INTO files_annotations(id,subject_kind,source_id,relative_path) VALUES(1,'file','source','one')")
        db.execute("""INSERT INTO files_annotation_attachments
          (id,annotation_id,content_hash,file_name,media_type,size,stored_root)
          VALUES(1,1,?,'original.png','image/png',?,?)""",(digest,len(data),str(original)));db.commit()
    registry=ModuleRegistry((descriptor(home,'test'),))
    return home,path,database,registry


def test_referenced_snapshot_copies_original_without_rewriting_owner_rows(case,tmp_path):
    home,path,database,registry=case;before=path.read_bytes()
    destination=tmp_path/'snapshot'
    value=assets.snapshot(database,home,registry,SimpleNamespace(),destination)
    assert len(value['files'])==1 and value['missing']==[]
    assert path.read_bytes()==before
    assert assets.validate_snapshot(destination)['files']==value['files']
    with closing(sqlite3.connect(database)) as db:
        assert assets.resolve(db,home,'files','attachment:1',expected_source=path).read_bytes()==before


def test_snapshot_restores_into_new_home_without_original_drive(case,tmp_path):
    home,path,database,registry=case
    destination=tmp_path/'snapshot';assets.snapshot(database,home,registry,SimpleNamespace(),destination)
    new_home=tmp_path/'fresh home';new_home.mkdir()
    assert assets.restore_snapshot(destination,new_home)=={'restored':1,'missing':0}
    assert list((new_home/'assets/files/originals').iterdir())[0].read_bytes()==path.read_bytes()


def test_changed_captured_bytes_refuse_without_false_complete_snapshot(case,tmp_path):
    home,path,database,registry=case;path.write_bytes(b'wrong original bytes')
    with pytest.raises(ValueError):assets.snapshot(database,home,registry,SimpleNamespace(),tmp_path/'snapshot')
    assert path.read_bytes()==b'wrong original bytes'


def test_missing_reference_is_reported_without_inventing_bytes(case,tmp_path):
    home,path,database,registry=case
    path.rename(path.with_name('unavailable-original'))
    value=assets.snapshot(database,home,registry,SimpleNamespace(),tmp_path/'snapshot')
    assert value['files']==[] and value['missing']==[{'owner':'files','key':'attachment:1'}]


def test_conflicting_preserved_target_is_never_overwritten(case,tmp_path):
    home,path,database,registry=case
    destination=tmp_path/'snapshot';value=assets.snapshot(database,home,registry,SimpleNamespace(),destination)
    new_home=tmp_path/'fresh';target=new_home/value['files'][0]['path'];target.parent.mkdir(parents=True)
    target.write_bytes(b'conflicting existing data')
    with pytest.raises(ValueError):assets.restore_snapshot(destination,new_home)
    assert target.read_bytes()==b'conflicting existing data'


def test_foreign_windows_origin_resolves_preserved_bytes_without_reading_old_drive(case,tmp_path):
    home,path,database,registry=case
    assets.snapshot(database,home,registry,SimpleNamespace(),tmp_path/'snapshot')
    with closing(sqlite3.connect(database)) as db:
        db.execute("UPDATE kv_preserved_assets SET original_path=?",(r'C:\Library\.keivotos\attachments\original.png',))
        db.commit()
        resolved=assets.resolve(db,home,'files','attachment:1',
                                expected_source='c:/library/.keivotos/attachments/original.png')
        assert resolved.read_bytes()==path.read_bytes()
        assert assets.resolve(db,home,'files','attachment:1',expected_source='D:/other/original.png') is None

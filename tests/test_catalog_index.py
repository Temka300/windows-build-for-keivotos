"""Disposable SQLite evidence, candidate recovery and ownership regressions."""
from contextlib import closing, contextmanager
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'backend'))
from services import catalog_index as catalog
from modules.danbooru.catalog_index import (
    index_owner, prepare_replacement, install_replacement,
)
from modules.danbooru.schema import ensure_catalog_index_schema
from files_base.schema import ensure_files_schema


@pytest.fixture
def stores(tmp_path):
    user = tmp_path/'user.sqlite'
    with closing(sqlite3.connect(user)) as db:
        db.execute('CREATE TABLE personal(id INTEGER PRIMARY KEY,note TEXT)')
        db.execute("INSERT INTO personal VALUES(1,'한국어')")
        db.commit()

    @contextmanager
    def essential():
        with closing(sqlite3.connect(user)) as db:
            db.row_factory = lambda cursor,row:dict(zip([c[0] for c in cursor.description],row))
            yield db
    return user, essential


def toy(owner='files'):
    def initialize(db):
        db.execute('CREATE TABLE IF NOT EXISTS observed(id INTEGER PRIMARY KEY,value TEXT)')
        db.commit()
    return catalog.IndexOwner(owner, ('observed',), initialize, lambda build:build.check())


def prepare(tmp_path, essential, owners, legacy=None, archives=None, cancelled=lambda:False, name='attempt'):
    return catalog.prepare_index(tmp_path/name, essential_database=essential, owners=owners,
        legacy_paths=legacy or {}, sources=(), suite_home=tmp_path/'home',
        lock_directory=tmp_path/'state', archives=archives, cancelled=cancelled)


@pytest.mark.parametrize('factory',[None,sqlite3.Row,
    lambda cursor,row:dict(zip([c[0] for c in cursor.description],row))])
def test_typed_preservation_unknown_columns_and_exact_replay(stores, tmp_path, factory):
    user, essential = stores
    with closing(sqlite3.connect(tmp_path/'old.sqlite')) as source:
        source.executescript('CREATE TABLE observed(id INTEGER PRIMARY KEY,value TEXT,"unknown 값" BLOB,real REAL);'
                             'CREATE INDEX value_index ON observed(value);')
        source.execute('INSERT INTO observed VALUES(?,?,?,?)',(17,' exact \0 한글 ',b'\0\xff',-0.0))
        source.commit()
        source.row_factory=factory
        with essential() as db:
            identity=catalog.preserve_owner(db,source,toy())
            assert catalog.preserve_owner(db,source,toy())==identity
            with closing(sqlite3.connect(':memory:')) as copy:
                catalog.restore_owner(db,copy,identity,toy())
                assert copy.execute('SELECT * FROM observed').fetchone()==(17,' exact \0 한글 ',b'\0\xff',0.0)
                assert catalog._owned_manifest(copy,('observed',))==catalog._owned_manifest(source,('observed',))
            assert db.execute('SELECT COUNT(*) AS n FROM kv_index_archives').fetchone()['n']==1
            assert db.execute('SELECT note FROM personal').fetchone()['note']=='한국어'


def test_preservation_refuses_pending_transactions_and_triggers(stores):
    _, essential=stores
    with closing(sqlite3.connect(':memory:')) as source,essential() as user:
        toy().initialize(source)
        source.execute("INSERT INTO observed VALUES(1,'pending')")
        with pytest.raises(ValueError,match='clean'):
            catalog.preserve_owner(user,source,toy())
        assert source.in_transaction
        source.rollback()
        source.execute('CREATE TRIGGER kept AFTER INSERT ON observed BEGIN SELECT 1; END')
        with pytest.raises(ValueError,match='trigger'):
            catalog.preserve_owner(user,source,toy())
        assert source.execute("SELECT name FROM sqlite_master WHERE name='kept'").fetchone()


def test_changed_preservation_rows_refuse_reconstruction(stores,tmp_path):
    _, essential=stores
    with closing(sqlite3.connect(':memory:')) as source, essential() as db:
        toy().initialize(source)
        source.execute("INSERT INTO observed VALUES(1,'kept')");source.commit()
        identity=catalog.preserve_owner(db,source,toy())
        db.execute('UPDATE kv_index_archive_rows SET payload=?',('[]',));db.commit()
        with pytest.raises((ValueError,sqlite3.Error)):
            catalog.verify_archive(db,identity,toy())


def test_combined_candidate_replay_and_rebuild_without_old_database(stores,tmp_path):
    _, essential=stores
    old=tmp_path/'old.sqlite'
    with closing(sqlite3.connect(old)) as db:
        toy().initialize(db);db.execute("INSERT INTO observed VALUES(77,'captured')");db.commit()
    result=prepare(tmp_path,essential,[toy()],{'files':old})
    again=prepare(tmp_path,essential,[toy()],{'files':old})
    assert result==again
    # The old derived DB can be absent: typed preservation is now the source.
    old.rename(tmp_path/'retained-old.sqlite')
    rebuilt=prepare(tmp_path,essential,[toy()],archives=result.archives,name='rebuild')
    with closing(sqlite3.connect(rebuilt.candidate)) as db:
        assert db.execute('SELECT * FROM observed').fetchall()==[(77,'captured')]
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='personal'").fetchone()
        catalog.validate_index(db,[toy()])


def test_cancelled_candidate_retains_evidence_and_retries(stores,tmp_path):
    _, essential=stores
    with pytest.raises(InterruptedError):
        prepare(tmp_path,essential,[toy()],cancelled=lambda:True)
    assert not (tmp_path/'attempt/index.sqlite').exists()
    assert list((tmp_path/'attempt').glob('*.partial.sqlite'))
    result=prepare(tmp_path,essential,[toy()])
    assert result.candidate.exists()
    assert list((tmp_path/'attempt').glob('*.partial.sqlite'))


def test_personal_or_legacy_changes_stop_acceptance(stores,tmp_path):
    _, essential=stores
    def changed(build):
        build.essential.execute("UPDATE personal SET note='newer write'")
        build.essential.commit()
    with pytest.raises(ValueError,match='Essential records changed'):
        prepare(tmp_path,essential,[replace(toy(),rebuild=changed)])
    assert not (tmp_path/'attempt/index.sqlite').exists()
    with essential() as db:
        assert db.execute('SELECT note FROM personal').fetchone()['note']=='newer write'
    legacy=tmp_path/'old.sqlite'
    with closing(sqlite3.connect(legacy)) as db:toy().initialize(db)
    def disk_changed(build):
        with closing(sqlite3.connect(legacy)) as db:
            db.execute("INSERT INTO observed VALUES(1,'new fact')");db.commit()
    with pytest.raises(ValueError,match='Legacy index changed'):
        prepare(tmp_path,essential,[replace(toy(),rebuild=disk_changed)],{'files':legacy},name='changed')
    assert not (tmp_path/'changed/index.sqlite').exists()


def test_changed_candidate_selection_and_unknown_schema_refuse(stores,tmp_path):
    _, essential=stores
    result=prepare(tmp_path,essential,[toy()])
    with closing(sqlite3.connect(result.candidate)) as db:
        db.execute('UPDATE kv_index_meta SET version=99');db.commit()
    with pytest.raises(ValueError,match='Unsupported'):
        prepare(tmp_path,essential,[toy()])
    with pytest.raises(ValueError,match='Conflicting'):
        prepare(tmp_path,essential,[toy(),toy('other')],name='collision')


def test_owner_registration_omits_disabled_and_orders_dependencies(tmp_path):
    from module_registry import build_registry
    registry=build_registry(tmp_path,'fixture')
    owners=catalog.owners_for(registry,set())
    assert [v.slug for v in owners]==['files']
    owners=catalog.owners_for(registry,{'ost','danbooru'})
    assert {v.slug for v in owners}=={'files','ost','danbooru'}
    assert [v.slug for v in owners].index('files')<[v.slug for v in owners].index('ost')
    assert 'ingest_state' not in {t for v in owners for t in v.tables}


def test_combined_schema_contains_no_operational_or_essential_tables(stores,tmp_path):
    _, essential=stores
    from modules.files import _catalog_index
    result=prepare(tmp_path,essential,[_catalog_index(),index_owner()])
    with closing(sqlite3.connect(result.candidate)) as db:
        catalog.validate_index(db,[_catalog_index(),index_owner()])
        names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not {'ingest_state','kv_operations','personal','youtube_downloads'}&names
        from modules.danbooru.pipeline import connect_sqlite,init_sqlite
        with closing(connect_sqlite(result.candidate)) as connection:
            init_sqlite(connection)
            assert connection.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
            catalog.validate_index(connection)


def seed_danbooru(path):
    with closing(sqlite3.connect(path)) as db:
        ensure_catalog_index_schema(db)
        db.execute("INSERT INTO files(id,path,name,downloaded_at) VALUES(51,'/offline/old.jpg','old.jpg','known old date')")
        db.execute("INSERT INTO posts(id,file_id,raw_json,rating) VALUES(71,51,'{\"unknown\":\"kept\"}','s')")
        db.execute("INSERT INTO metadata VALUES('unknown_migration','exact evidence')")
        db.commit()


def test_danbooru_replacement_preserves_other_owners_and_complete_old_generation(stores,tmp_path):
    user,_=stores
    live=tmp_path/'combined.sqlite'
    seed_danbooru(live)
    with closing(sqlite3.connect(live)) as db:
        ensure_files_schema(db)
        db.execute("INSERT INTO files_index(source_id,path,relative_path,parent,name) VALUES('s','/music/a.mp3','a.mp3','','a.mp3')")
        db.execute('CREATE TABLE ingest_state(kept TEXT)')
        db.execute("INSERT INTO ingest_state VALUES('preserved job evidence')")
        db.commit()
    replacement=prepare_replacement(live,user)
    with closing(sqlite3.connect(replacement.candidate)) as db:
        db.execute('UPDATE posts SET score=123');db.commit()
    install_replacement(replacement)
    with closing(sqlite3.connect(live)) as db:
        assert db.execute('SELECT score,raw_json FROM posts').fetchone()==(123,'{"unknown":"kept"}')
        assert db.execute('SELECT downloaded_at FROM files').fetchone()[0]=='known old date'
        assert db.execute('SELECT name FROM files_index').fetchone()[0]=='a.mp3'
        assert db.execute('SELECT kept FROM ingest_state').fetchone()[0]=='preserved job evidence'
    assert (replacement.recovery/'previous.sqlite').exists()
    with closing(sqlite3.connect(user)) as db:
        assert db.execute("SELECT owner FROM kv_index_archives").fetchone()[0]=='danbooru'


def test_changed_live_danbooru_refuses_installation(stores,tmp_path):
    user,_=stores
    live=tmp_path/'index.sqlite';seed_danbooru(live)
    replacement=prepare_replacement(live,user)
    with closing(sqlite3.connect(live)) as db:
        db.execute("UPDATE metadata SET value='newer evidence'");db.commit()
    with pytest.raises(ValueError,match='changed during rebuild'):
        install_replacement(replacement)
    with closing(sqlite3.connect(live)) as db:
        assert db.execute('SELECT value FROM metadata').fetchone()[0]=='newer evidence'


def test_preservation_keeps_autoincrement_high_watermark(stores):
    _,essential=stores
    with closing(sqlite3.connect(':memory:')) as source, essential() as user:
        source.execute('CREATE TABLE observed(id INTEGER PRIMARY KEY AUTOINCREMENT,value TEXT)')
        source.executemany('INSERT INTO observed(value) VALUES(?)',[('first',),('retired',)])
        source.execute('DELETE FROM observed WHERE id=2');source.commit()
        identity=catalog.preserve_owner(user,source,toy())
        with closing(sqlite3.connect(':memory:')) as rebuilt:
            catalog.restore_owner(user,rebuilt,identity,toy())
            rebuilt.execute("INSERT INTO observed(value) VALUES('new')")
            assert rebuilt.execute('SELECT max(id) FROM observed').fetchone()[0]==3


def test_explicit_enable_registers_only_the_new_owner(stores,tmp_path):
    _,essential=stores
    from modules.files import _catalog_index
    from modules.danbooru.schema import ensure_data_schema
    result=prepare(tmp_path,essential,[_catalog_index()])
    with closing(sqlite3.connect(result.candidate)) as db:
        assert db.execute('SELECT owner FROM kv_index_owners').fetchall()==[('files',)]
        ensure_data_schema(db)
        assert {row[0] for row in db.execute('SELECT owner FROM kv_index_owners')}=={'files','danbooru'}
        assert not db.execute("SELECT name FROM sqlite_master WHERE name IN ('ingest_state','ost_catalog')").fetchone()
        catalog.validate_index(db,[_catalog_index(),index_owner()])


def test_profile_preparation_lock_prevents_concurrent_attempts(stores,tmp_path):
    _,essential=stores
    from services.data_migrations import _attempt_lock,PreparationError
    state=tmp_path/'state';state.mkdir()
    with _attempt_lock(state):
        with pytest.raises(PreparationError,match='busy'):
            prepare(tmp_path,essential,[toy()])
    assert not (tmp_path/'attempt/index.sqlite').exists()


def test_unclassified_source_table_refuses_without_discarding_its_rows(stores):
    _,essential=stores
    with closing(sqlite3.connect(':memory:')) as source, essential() as user:
        toy().initialize(source)
        source.execute('CREATE TABLE unknown_personal(note TEXT)')
        source.execute("INSERT INTO unknown_personal VALUES('sole evidence')");source.commit()
        with pytest.raises(ValueError,match='Unclassified'):
            catalog.preserve_owner(user,source,toy())
        assert source.execute('SELECT note FROM unknown_personal').fetchone()[0]=='sole evidence'

def sidecar_case(tmp_path, essential):
    media=tmp_path/'media';media.mkdir()
    asset=media/'sample.jpg';asset.write_bytes(b'untouched original')
    sidecar=media/'sample.jpg.danbooru.json'
    payload={'local_file':{'path':str(asset),'size':18},
             'post':{'id':81,'rating':'g','tag_string_general':'saved_tag','source':'https://example.invalid/original'},
             'tags':{'general':['saved_tag']}}
    sidecar.write_text(json.dumps(payload))
    return media,asset,sidecar


def test_sidecar_only_rebuild_preserves_new_facts_and_unknown_dates(stores,tmp_path):
    _,essential=stores
    media,asset,sidecar=sidecar_case(tmp_path,essential)
    owner=index_owner(media_root=media)
    original=asset.read_bytes()
    first=prepare(tmp_path,essential,[owner])
    with closing(sqlite3.connect(first.candidate)) as db:
        expected=db.execute('SELECT id,path,downloaded_at FROM files').fetchall()
        assert expected==[(1,str(asset),None)]
        assert db.execute('SELECT danbooru_post_id,source_url FROM posts').fetchone()==(81,'https://example.invalid/original')
        assert db.execute('SELECT name FROM tags').fetchone()==('saved_tag',)
    # Retain originals; only generated recovery inputs are taken offline.
    sidecar.rename(sidecar.with_suffix('.retained'))
    first.candidate.rename(first.candidate.with_suffix('.retained'))
    rebuilt=prepare(tmp_path,essential,[owner],archives=first.archives,name='without-old-index-or-sidecar')
    with closing(sqlite3.connect(rebuilt.candidate)) as db:
        assert db.execute('SELECT id,path,downloaded_at FROM files').fetchall()==expected
        assert db.execute('SELECT name FROM tags').fetchone()==('saved_tag',)
    assert asset.read_bytes()==original


def test_recovered_sidecar_never_reuses_unresolved_personal_identity(stores,tmp_path):
    user,essential=stores
    with closing(sqlite3.connect(user)) as db:
        db.execute('CREATE TABLE favorites(file_id INTEGER PRIMARY KEY,file_path TEXT)')
        db.execute("INSERT INTO favorites VALUES(51,'/unavailable/old.jpg')")
        db.commit()
    media,asset,_=sidecar_case(tmp_path,essential)
    result=prepare(tmp_path,essential,[index_owner(media_root=media)])
    with closing(sqlite3.connect(result.candidate)) as db:
        assert db.execute('SELECT id FROM files').fetchone()==(52,)
    with closing(sqlite3.connect(user)) as db:
        assert db.execute('SELECT * FROM favorites').fetchone()==(51,'/unavailable/old.jpg')


def test_sidecar_revision_change_refuses_candidate(stores,tmp_path):
    _,essential=stores
    media,asset,sidecar=sidecar_case(tmp_path,essential)
    owner=index_owner(media_root=media)
    def changed(build):
        owner.rebuild(build)
        sidecar.write_text(sidecar.read_text()+' ')
    with pytest.raises(ValueError,match='source files changed'):
        prepare(tmp_path,essential,[replace(owner,rebuild=changed)])
    assert not (tmp_path/'attempt/index.sqlite').exists()
    assert asset.exists()


def test_sidecar_unregistered_media_refused_without_touching_original(stores,tmp_path):
    _,essential=stores
    media,asset,sidecar=sidecar_case(tmp_path,essential)
    outside=tmp_path/'outside.jpg';outside.write_bytes(b'preserved')
    payload=json.loads(sidecar.read_text());payload['local_file']['path']=str(outside)
    sidecar.write_text(json.dumps(payload))
    with pytest.raises(ValueError,match='unregistered root'):
        prepare(tmp_path,essential,[index_owner(media_root=media)])
    assert outside.read_bytes()==b'preserved'


def test_linked_sidecar_refused(stores,tmp_path):
    _,essential=stores
    media,asset,sidecar=sidecar_case(tmp_path,essential)
    original=tmp_path/'source.json';sidecar.rename(original)
    try:sidecar.symlink_to(original)
    except OSError:pytest.skip('symlinks unavailable')
    with pytest.raises(ValueError,match='linked'):
        prepare(tmp_path,essential,[index_owner(media_root=media)])
    assert original.exists() and asset.exists()



def test_unclassified_view_refuses_preservation(stores):
    _,essential=stores
    with essential() as user,closing(sqlite3.connect(':memory:')) as source:
        toy().initialize(source)
        source.execute('CREATE VIEW unknown_source_fact AS SELECT * FROM observed')
        with pytest.raises(ValueError,match='view'):
            catalog.preserve_owner(user,source,toy())
        assert source.execute("SELECT 1 FROM sqlite_master WHERE name='unknown_source_fact'").fetchone()


def test_completed_candidate_source_selection_cannot_be_modified(stores,tmp_path):
    _,essential=stores
    result=prepare(tmp_path,essential,[toy()])
    with closing(sqlite3.connect(result.candidate)) as db:
        db.execute("UPDATE kv_index_meta SET selection='{}'");db.commit()
    with pytest.raises(ValueError,match='source selection'):
        prepare(tmp_path,essential,[toy()])



@pytest.mark.parametrize('ambiguous',[False,True])
def test_canonical_sidecar_uses_registered_root_alias_without_reassigning_ids(stores,tmp_path,ambiguous):
    user,essential=stores
    root=tmp_path/'primary';root.mkdir()
    relocated=tmp_path/'relocated';relocated.mkdir()
    media=relocated/'sample.jpg';media.write_bytes(b'preserved original')
    old_path=tmp_path/'old-location/sample.jpg'
    old=tmp_path/'legacy.sqlite';seed_danbooru(old)
    with closing(sqlite3.connect(old)) as db:
        db.execute("UPDATE files SET path=?,root_id='root-stable',relative_path='sample.jpg'",(str(old_path),))
        if ambiguous:
            db.execute("INSERT INTO files(id,path,name,root_id,relative_path) VALUES(52,?,'other','root-stable','sample.jpg')",
                       (str(tmp_path/'second-old-location/sample.jpg'),))
        db.commit()
    with closing(sqlite3.connect(user)) as db:
        db.execute('CREATE TABLE registered_folders(name TEXT,path TEXT,root_id TEXT,display_name TEXT)')
        db.execute("INSERT INTO registered_folders VALUES('Relocated',?,'root-stable','Relocated')",(str(relocated),))
        db.commit()
    metadata=tmp_path/'sidecars'
    sidecar=metadata/'roots/root-stable/sample.jpg.danbooru.json';sidecar.parent.mkdir(parents=True)
    sidecar.write_text(json.dumps({'local_file':{'path':str(old_path)},'post':{'id':81},
                                   'tags':{'general':['saved_tag']}}))
    original=sidecar.read_bytes()
    owner=index_owner(media_root=root,sidecar_dir=metadata)
    if ambiguous:
        with pytest.raises(ValueError,match='Ambiguous'):
            prepare(tmp_path,essential,[owner],legacy={'danbooru':old})
    else:
        result=prepare(tmp_path,essential,[owner],legacy={'danbooru':old})
        with closing(sqlite3.connect(result.candidate)) as db:
            assert db.execute('SELECT id,path,downloaded_at FROM files').fetchone()==(51,str(media),'known old date')
        assert prepare(tmp_path,essential,[owner],legacy={'danbooru':old})==result
    assert sidecar.read_bytes()==original and media.read_bytes()==b'preserved original'



def test_partial_sidecar_cannot_erase_preserved_hash_tags_or_raw_facts(stores,tmp_path):
    _,essential=stores
    media,asset,sidecar=sidecar_case(tmp_path,essential)
    old=tmp_path/'legacy.sqlite';seed_danbooru(old)
    with closing(sqlite3.connect(old)) as db:
        db.execute("UPDATE files SET path=?,local_md5='known-content-hash'",(str(asset),))
        db.execute("INSERT INTO tags(id,name,category) VALUES(1,'original_tag','general')")
        db.execute('INSERT INTO post_tags(post_id,tag_id) VALUES(71,1)');db.commit()
    result=prepare(tmp_path,essential,[index_owner(media_root=media)],legacy={'danbooru':old})
    with closing(sqlite3.connect(result.candidate)) as db:
        assert db.execute('SELECT local_md5 FROM files').fetchone()==('known-content-hash',)
        assert db.execute('SELECT raw_json FROM posts').fetchone()==('{"unknown":"kept"}',)
        assert db.execute('SELECT name FROM tags').fetchall()==[('original_tag',)]
        source=json.loads(db.execute("SELECT value FROM metadata WHERE key LIKE 'catalog-source:%'").fetchone()[0])
        import base64
        assert base64.b64decode(source['base64'])==sidecar.read_bytes()


def test_root_rebinding_refuses_to_break_authored_path_relationships(stores,tmp_path):
    user,essential=stores
    media,asset,sidecar=sidecar_case(tmp_path,essential)
    old_path=tmp_path/'old/sample.jpg'
    old=tmp_path/'legacy.sqlite';seed_danbooru(old)
    with closing(sqlite3.connect(old)) as db:
        db.execute("UPDATE files SET path=?,root_id='portable-root',relative_path='sample.jpg'",(str(old_path),));db.commit()
    # Use a canonical portable-root sidecar at the current root.
    metadata=tmp_path/'sidecars';canonical=metadata/'roots/portable-root/sample.jpg.danbooru.json'
    canonical.parent.mkdir(parents=True);canonical.write_bytes(sidecar.read_bytes());sidecar.rename(sidecar.with_suffix('.retained'))
    with closing(sqlite3.connect(user)) as db:
        db.execute('CREATE TABLE favorites(file_id INTEGER PRIMARY KEY,file_path TEXT)')
        db.execute('INSERT INTO favorites VALUES(51,?)',(str(old_path),));db.commit()
    with pytest.raises(ValueError,match='coordinated relocation'):
        prepare(tmp_path,essential,[index_owner(media_root=media,sidecar_dir=metadata)],legacy={'danbooru':old})
    with closing(sqlite3.connect(user)) as db:
        assert db.execute('SELECT file_path FROM favorites').fetchone()==(str(old_path),)
    with closing(sqlite3.connect(old)) as db:
        assert db.execute('SELECT path FROM files').fetchone()==(str(old_path),)

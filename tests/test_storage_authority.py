"""Coordinated startup copies, interruption recovery and unchanged originals."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from module_registry import ModuleRegistry
from modules.files import descriptor
from files_base import sources,index
from services import storage_authority as authority
from services import item_catalog


def profile(tmp_path):
    home=tmp_path/'home';home.mkdir()
    legacy=home/'user.sqlite'
    with closing(sqlite3.connect(legacy)) as db:
        db.executescript("CREATE TABLE user_settings(key TEXT PRIMARY KEY,value TEXT);"
                         "INSERT INTO user_settings VALUES('authored','한글');"
                         "CREATE TABLE suite_enabled_modules(module TEXT PRIMARY KEY);")
        db.row_factory=sqlite3.Row
        sources.ensure_sources_schema(db)
    old_index=home/'base/files.sqlite'
    with index.open_index(old_index):pass
    registry=ModuleRegistry((descriptor(home,'test'),))
    return home,legacy,old_index,registry


def activate(home,legacy,old_index,registry,**kw):
    return authority.prepare_layout(home,registry,legacy_user=legacy,
        essential=home/'keivotos.sqlite',index=home/'indexes/index.sqlite',
        jobs=home/'state/jobs.sqlite',legacy_indexes={'files':old_index},**kw)


def test_activation_copies_authored_records_and_is_exactly_retryable(tmp_path):
    home,legacy,old_index,registry=profile(tmp_path)
    before={p:authority.fingerprint(p) for p in (legacy,old_index)}
    result=activate(home,legacy,old_index,registry)
    assert result==activate(home,legacy,old_index,registry)
    assert all(authority.fingerprint(p)==s for p,s in before.items())
    with authority.connection(home/'keivotos.sqlite',readonly=True) as db:
        assert db.execute("SELECT value FROM user_settings").fetchone()[0]=='한글'
    with authority.connection(home/'indexes/index.sqlite',readonly=True) as db:
        assert db.execute('SELECT owner FROM kv_index_owners').fetchone()[0]=='files'


@pytest.mark.parametrize('phase',['essential','jobs','index','published-essential','published-index','published-jobs','activated'])
def test_each_activation_boundary_recovers_without_replacing_legacy(tmp_path,phase):
    home,legacy,old_index,registry=profile(tmp_path)
    before=authority.fingerprint(legacy)
    def interrupted(at):
        if at==phase:raise OSError('interrupted fixture')
    with pytest.raises(OSError,match='interrupted fixture'):
        activate(home,legacy,old_index,registry,after_phase=interrupted)
    result=activate(home,legacy,old_index,registry)
    assert result['format']==1 and authority.fingerprint(legacy)==before


def test_conflicting_target_is_preserved_and_refused(tmp_path):
    home,legacy,old_index,registry=profile(tmp_path)
    target=home/'keivotos.sqlite'
    with closing(sqlite3.connect(target)) as db:db.execute('CREATE TABLE existing_user_data(value)')
    before=target.read_bytes()
    with pytest.raises(ValueError,match='conflicts'):activate(home,legacy,old_index,registry)
    assert target.read_bytes()==before and legacy.is_file()


def test_legacy_changed_after_activation_is_not_silently_ignored(tmp_path):
    home,legacy,old_index,registry=profile(tmp_path);activate(home,legacy,old_index,registry)
    with closing(sqlite3.connect(legacy)) as db:
        db.execute("INSERT INTO user_settings VALUES('later','old app write')");db.commit()
    with pytest.raises(ValueError,match='Legacy source changed'):activate(home,legacy,old_index,registry)


def test_initial_copy_does_not_scan_registered_originals(tmp_path,monkeypatch):
    home,legacy,old_index,registry=profile(tmp_path)
    monkeypatch.setattr(index,'rebuild_index',lambda *_:pytest.fail('Startup scanned original media'))
    activate(home,legacy,old_index,registry)


def test_fresh_home_without_legacy_starts_and_retains_no_fake_data(tmp_path):
    home=tmp_path/'fresh';registry=ModuleRegistry((descriptor(home,'test'),))
    result=activate(home,home/'user.sqlite',home/'base/files.sqlite',registry)
    assert result['format']==1 and not (home/'user.sqlite').exists()
    with authority.connection(home/'keivotos.sqlite',readonly=True) as db:
        assert db.execute('SELECT count(*) FROM files_sources').fetchone()[0]==0


def test_unknown_active_receipt_refuses_without_repair(tmp_path):
    home,legacy,old_index,registry=profile(tmp_path);activate(home,legacy,old_index,registry)
    marker=home/'state/storage-active.json';marker.write_text('{"format":99}')
    with pytest.raises(ValueError,match='Unsupported'):activate(home,legacy,old_index,registry)
    assert marker.read_text()=='{"format":99}'


@pytest.mark.parametrize('boundary',['installed-essential','installed-index'])
def test_multistore_install_restarts_forward_from_each_partial_boundary(tmp_path,boundary):
    home,legacy,old_index,registry=profile(tmp_path);activate(home,legacy,old_index,registry)
    attempt=home/'recovery/new-generation';attempt.mkdir(parents=True)
    targets=[home/'keivotos.sqlite',home/'indexes/index.sqlite']
    from services.data_migrations import snapshot_user_database
    for name,target in zip(('essential','index'),targets):
        snapshot_user_database(target,attempt/(name+'.sqlite'))
        with closing(sqlite3.connect(attempt/(name+'.sqlite'))) as db:
            db.execute('CREATE TABLE new_generation(value)');db.commit()
    def interrupted(phase):
        if phase==boundary:raise OSError('lost process fixture')
    with pytest.raises(OSError):
        authority.install_databases(home,attempt,[(name,attempt/(name+'.sqlite'),target)
            for name,target in zip(('essential','index'),targets)],after_phase=interrupted)
    assert (home/'state/storage-install-pending.json').exists()
    assert authority.resume_install(home)['status']=='installed'
    assert not (home/'state/storage-install-pending.json').exists()
    for target in targets:
        with authority.connection(target,readonly=True) as db:
            assert db.execute("SELECT 1 FROM sqlite_master WHERE name='new_generation'").fetchone()


def test_multistore_rollback_retains_new_and_old_database_versions(tmp_path):
    home,legacy,old_index,registry=profile(tmp_path);activate(home,legacy,old_index,registry)
    target=home/'keivotos.sqlite';before=authority.fingerprint(target)
    attempt=home/'recovery/rejected';attempt.mkdir(parents=True)
    from services.data_migrations import snapshot_user_database
    snapshot_user_database(target,attempt/'essential.sqlite')
    with closing(sqlite3.connect(attempt/'essential.sqlite')) as db:
        db.execute("UPDATE user_settings SET value='new value'");db.commit()
    def interrupted(phase):raise OSError('lost reply')
    with pytest.raises(OSError):
        authority.install_databases(home,attempt,[('essential',attempt/'essential.sqlite',target)],after_phase=interrupted)
    assert authority.rollback_install(home)['status']=='rolled_back'
    assert authority.fingerprint(target)==before
    assert (attempt/'essential.sqlite').is_file() and (attempt/'original-essential.sqlite').is_file()


@pytest.mark.parametrize('phase',['installed-essential','installed-notes'])
def test_database_and_referenced_history_resume_as_one_generation(tmp_path,phase):
    home,legacy,old_index,registry=profile(tmp_path)
    activate(home,legacy,old_index,registry)
    attempt=home/'recovery/restore';attempt.mkdir(parents=True)
    candidate=attempt/'essential.sqlite'
    authority.migration.snapshot_user_database(home/'keivotos.sqlite',candidate)
    with authority.connection(candidate) as db:db.execute("UPDATE user_settings SET value='restored'")
    original=home/'assets/language/revisions';original.mkdir(parents=True)
    (original/'before.md').write_bytes(b'current history')
    incoming=attempt/'notes';incoming.mkdir()
    (incoming/'before.md').write_bytes(b'backup history')
    def stop(at):
        if at==phase:raise OSError('power loss')
    with pytest.raises(OSError,match='power loss'):
        authority.install_databases(home,attempt,[('essential',candidate,home/'keivotos.sqlite')],
            resources=[('notes',incoming,original)],after_phase=stop)
    assert (home/'state/storage-install-pending.json').is_file()
    authority.resume_install(home)
    assert (original/'before.md').read_bytes()==b'backup history'
    assert (attempt/'original-resource-notes/before.md').read_bytes()==b'current history'
    with authority.connection(home/'keivotos.sqlite',readonly=True) as db:
        assert db.execute('SELECT value FROM user_settings').fetchone()[0]=='restored'


def test_resource_rollback_keeps_both_history_versions(tmp_path):
    home=tmp_path/'home';home.mkdir()
    attempt=home/'recovery/restore';attempt.mkdir(parents=True)
    old=home/'config.json';old.write_bytes(b'old configuration')
    new=attempt/'config.json';new.write_bytes(b'new configuration')
    def stop(_):raise OSError('power loss')
    with pytest.raises(OSError):
        authority.install_databases(home,attempt,[],resources=[('config',new,old)],after_phase=stop)
    authority.rollback_install(home)
    assert old.read_bytes()==b'old configuration'
    assert new.read_bytes()==b'new configuration'
    assert (attempt/'original-resource-config').read_bytes()==b'old configuration'


def test_missing_derived_file_restores_saved_facts_without_media_scan(tmp_path):
    home,legacy,old_index,registry=profile(tmp_path)
    root=tmp_path/'Publisher album';root.mkdir();(root/'original.txt').write_bytes(b'untouched')
    with authority.connection(legacy) as db:
        registered=sources.register_source(db,root,'Publisher album')
    with index.open_index(old_index) as db:index.scan_source(db,registered.source_id,root)
    activate(home,legacy,old_index,registry)
    active=home/'indexes/index.sqlite';retained=home/'recovery/retained-index.sqlite'
    retained.parent.mkdir(exist_ok=True);active.rename(retained)
    configuration=SimpleNamespace(SUITE_HOME=home,USER_DB_PATH=home/'keivotos.sqlite',
                                  INDEX_DB_PATH=active,MODULE_REGISTRY=registry)
    authority.restore_missing_index(configuration)
    with authority.connection(active,readonly=True) as db:
        assert db.execute('SELECT name FROM files_index WHERE is_dir=0').fetchone()[0]=='original.txt'
    assert (root/'original.txt').read_bytes()==b'untouched' and retained.is_file()


def test_waiting_connection_refuses_a_generation_left_pending(tmp_path,monkeypatch):
    import threading,time,config,database_connections as connections
    home=tmp_path/'home';(home/'state').mkdir(parents=True)
    active=home/'keivotos.sqlite'
    monkeypatch.setattr(config,'SUITE_HOME',home);monkeypatch.setattr(config,'USER_DB_PATH',active)
    errors=[];started=threading.Event()
    def reader():
        started.set()
        try:
            with connections.connect_database(active):pass
        except RuntimeError as exc:errors.append(str(exc))
    with connections.exclusive_database_access():
        worker=threading.Thread(target=reader);worker.start();assert started.wait(2)
        (home/'state/storage-install-pending.json').write_text('{}')
    worker.join(2)
    assert not worker.is_alive() and errors and not active.exists()

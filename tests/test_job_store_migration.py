"""Operational copy/restart faults use disposable SQLite files only."""
from contextlib import contextmanager, closing
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import pytest
from services import job_store_migration as migration, operations

def connect(path):
    connection=sqlite3.connect(path)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA foreign_keys=ON')
    return closing(connection)

@pytest.fixture
def stores(tmp_path):
    user=tmp_path/'user.sqlite';jobs=tmp_path/'jobs.sqlite'
    with connect(user) as db:
        db.execute('PRAGMA user_version=77')
        db.execute('CREATE TABLE owner_jobs(id TEXT PRIMARY KEY,state TEXT,unknown BLOB)')
        db.execute("INSERT INTO owner_jobs VALUES('original','queued',?)",(b'\x00\xff',))
        db.commit()
    return user,jobs

def prepare(user,jobs,**options):
    with connect(user) as source,connect(jobs) as target:
        migration.prepare(target,source,source,owner='test',table='owner_jobs',**options)

def test_copy_preserves_unknown_typed_fields_and_original_schema(stores):
    user,jobs=stores
    with connect(user) as db:
        before=db.execute('SELECT * FROM owner_jobs').fetchall()
        schema=db.execute("SELECT sql FROM sqlite_master WHERE name='owner_jobs'").fetchone()
    prepare(user,jobs);prepare(user,jobs)
    with connect(user) as db,connect(jobs) as target:
        assert db.execute('PRAGMA user_version').fetchone()==(77,)
        assert db.execute('SELECT * FROM owner_jobs').fetchall()==before
        assert db.execute("SELECT sql FROM sqlite_master WHERE name='owner_jobs'").fetchone()==schema
        assert target.execute('SELECT * FROM owner_jobs').fetchall()==before
        assert target.execute('PRAGMA integrity_check').fetchone()==('ok',)

def test_crash_after_job_copy_reuses_verified_rows(stores):
    user,jobs=stores
    def fail(db):raise RuntimeError('essential receipt interrupted')
    with pytest.raises(RuntimeError,match='receipt interrupted'):
        prepare(user,jobs,preserve=fail)
    with connect(jobs) as db:
        assert db.execute('SELECT count(*) FROM owner_jobs').fetchone()==(1,)
    prepare(user,jobs)
    with connect(user) as db,connect(jobs) as target:
        assert db.execute('SELECT count(*) FROM kv_operational_migrations').fetchone()==(1,)
        assert target.execute('SELECT count(*) FROM owner_jobs').fetchone()==(1,)

def test_history_loss_does_not_reimport_old_pending_work(stores):
    user,jobs=stores
    prepare(user,jobs)
    # Move the generated history aside, preserving the evidence exactly.
    archived=jobs.with_name('retained-jobs.sqlite')
    jobs.rename(archived)
    prepare(user,jobs)
    with connect(jobs) as db,connect(user) as source:
        assert db.execute('SELECT * FROM owner_jobs').fetchall()==[]
        assert source.execute('SELECT history_lost FROM kv_operational_migrations ORDER BY rowid').fetchall()==[(0,),(1,)]
    with connect(archived) as db:
        assert db.execute('SELECT id FROM owner_jobs').fetchall()==[('original',)]

def test_unsupported_operational_schema_refuses_without_repair(stores):
    user,jobs=stores
    prepare(user,jobs)
    with connect(jobs) as db:
        db.execute('UPDATE kv_operation_schema SET version=999');db.commit()
    with pytest.raises(ValueError,match='Unsupported operation'):
        prepare(user,jobs)
    with connect(jobs) as db:
        assert db.execute('SELECT version FROM kv_operation_schema').fetchone()==(999,)

def test_failed_source_schema_keeps_legacy_and_retryable_store(stores):
    user,jobs=stores
    with connect(user) as db:
        db.execute('ALTER TABLE owner_jobs RENAME TO preserved_jobs');db.commit()
    with pytest.raises(ValueError,match='Missing legacy'):
        prepare(user,jobs)
    with connect(user) as db,connect(jobs) as target:
        assert db.execute('SELECT id FROM preserved_jobs').fetchall()==[('original',)]
        assert target.execute('SELECT * FROM kv_operational_owners').fetchall()==[]
        db.execute('ALTER TABLE preserved_jobs RENAME TO owner_jobs');db.commit()
    prepare(user,jobs)

def test_concurrent_processes_copy_once(stores):
    import subprocess
    user,jobs=stores
    code="import sys,sqlite3;sys.path.insert(0,'backend');from services.job_store_migration import prepare;u=sqlite3.connect(sys.argv[1],timeout=10);j=sqlite3.connect(sys.argv[2],timeout=10);prepare(j,u,u,owner='test',table='owner_jobs')"
    processes=[subprocess.Popen([sys.executable,'-c',code,str(user),str(jobs)],stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(4)]
    for process in processes:
        out,error=process.communicate(timeout=20)
        assert process.returncode==0,error.decode()
    with connect(jobs) as db:
        assert db.execute('SELECT count(*) FROM owner_jobs').fetchone()==(1,)


def test_wrong_profile_cannot_adopt_another_owners_operational_store(stores,tmp_path):
    user,jobs=stores
    prepare(user,jobs)
    other=tmp_path/'other-user.sqlite'
    with connect(other) as db:
        db.execute('CREATE TABLE other_jobs(id TEXT PRIMARY KEY)')
        db.execute("INSERT INTO other_jobs VALUES('different source')");db.commit()
    with connect(other) as source,connect(jobs) as target:
        with pytest.raises(ValueError,match='different migration source'):
            migration.prepare(target,source,source,owner='other',table='other_jobs')
    with connect(jobs) as db:
        assert db.execute('SELECT id FROM owner_jobs').fetchall()==[('original',)]
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='other_jobs'").fetchone()


def test_pending_caller_write_is_not_committed_by_migration(stores):
    user,jobs=stores
    with connect(user) as source,connect(jobs) as target:
        source.execute("UPDATE owner_jobs SET state='caller pending'")
        with pytest.raises(ValueError,match='clean source'):
            migration.prepare(target,source,source,owner='test',table='owner_jobs')
        assert source.in_transaction
        source.rollback()
        assert source.execute('SELECT state FROM owner_jobs').fetchone()==('queued',)
        assert target.execute("SELECT name FROM sqlite_master").fetchall()==[]


def test_unicode_columns_and_owner_indexes_preserve_exact_legacy_values(stores):
    user,jobs=stores
    with connect(user) as db:
        db.execute('ALTER TABLE owner_jobs ADD COLUMN "captured 한국어" TEXT')
        db.execute('UPDATE owner_jobs SET "captured 한국어"=?',('한글\noriginal',))
        db.execute('CREATE INDEX owner_queue ON owner_jobs(state,id)')
        db.commit()
    prepare(user,jobs)
    with connect(jobs) as db:
        assert db.execute('SELECT "captured 한국어",unknown FROM owner_jobs').fetchall()==[('한글\noriginal',b'\x00\xff')]
        assert db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='owner_queue'").fetchone()==('owner_queue',)


def test_unknown_operational_triggers_refuse_without_removing_them(stores):
    user,jobs=stores
    with connect(user) as db:
        db.execute('CREATE TABLE audit(value TEXT)')
        db.execute("CREATE TRIGGER owner_audit AFTER INSERT ON owner_jobs BEGIN INSERT INTO audit VALUES(new.id); END")
        db.commit()
    with pytest.raises(ValueError,match='operational trigger'):
        prepare(user,jobs)
    with connect(user) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()==[('owner_audit',)]
        assert db.execute('SELECT id FROM owner_jobs').fetchall()==[('original',)]

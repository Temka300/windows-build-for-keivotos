"""Application ingest uses the operational ledger; generated media only."""
from contextlib import closing
from dataclasses import dataclass
import builtins
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
from uuid import uuid4

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
import config
import database
from modules.danbooru import pipeline, operational_state, tools
from modules.danbooru.catalog_index import index_owner
from services.catalog_index import INDEX_SCHEMA, _json
from storage_layout import LibraryRoot


@dataclass
class Ingest:
    base: Path
    media: Path
    index: Path
    jobs: Path
    user: Path

    def seed(self, count):
        for number in range(count):
            path = self.media / f'{number:04}.png'
            if not path.exists():
                Image.new('RGB', (24, 18), (number % 255, 20, 30)).save(path)

    def args(self, phase, *options):
        return pipeline.build_parser().parse_args([
            '--root', str(self.media), '--sidecar-dir', str(self.base / 'sidecars'),
            '--jobs-db', str(self.jobs), '--user-db', str(self.user), phase,
            '--output', str(self.index), *options, str(self.media),
        ])

    def ledger(self):
        with closing(sqlite3.connect(self.jobs)) as db, db:
            return db.execute('SELECT media_path,phase,status,error,enriched_at,metadata_at,finalized_at '
                              'FROM ingest_state ORDER BY media_path').fetchall()

    def originals(self):
        return {str(p.relative_to(self.media)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.media.rglob('*') if p.is_file()}


@pytest.fixture
def ingest(tmp_path, monkeypatch):
    media = tmp_path / 'Publisher 한글'; media.mkdir()
    case = Ingest(tmp_path, media, tmp_path / 'index.sqlite', tmp_path / 'jobs.sqlite', tmp_path / 'user.sqlite')
    case.seed(3)
    with closing(sqlite3.connect(case.user)) as db, db:
        db.execute('CREATE TABLE preserved_notes(value TEXT)')
        db.execute("INSERT INTO preserved_notes VALUES('authored original note')")
        db.execute('PRAGMA user_version=91')
    owner = index_owner()
    with closing(sqlite3.connect(case.index)) as db, db:
        owner.initialize(db); db.executescript(INDEX_SCHEMA)
        db.execute('INSERT INTO kv_index_meta VALUES(1,1,?,?)', ('a' * 32, '[]'))
        db.execute('INSERT INTO kv_index_owners VALUES(?,?,?)', (owner.slug, 1, _json(owner.tables)))
    for name, value in [('JOBS_DB_PATH', case.jobs), ('SUITE_HOME', tmp_path / 'profile'),
                        ('DATA_DB_PATH', case.index), ('INDEX_DB_PATH', case.index), ('USER_DB_PATH', case.user)]:
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(database, 'DATA_DB_PATH', case.index)
    monkeypatch.setattr(database, 'USER_DB_PATH', case.user)
    monkeypatch.setattr(pipeline, '_LIBRARY_ROOTS', [LibraryRoot('ingest-stable-root', media.name, media)])
    return case


def writer_available(case):
    with closing(sqlite3.connect(case.jobs, timeout=.2)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        db.rollback()


def prepare(case):
    assert pipeline.run_import_discover(case.args('import-discover')) == 0
    assert pipeline.run_import_enrich(case.args('import-enrich', '--workers', '2')) == 0


def test_combined_index_finalization_uses_jobs_and_repeats_safely(ingest):
    case = ingest; originals = case.originals(); prepare(case)
    assert pipeline.run_import_finalize(case.args('import-finalize')) == 0
    assert pipeline.run_import_finalize(case.args('import-finalize')) == 0
    assert all(row[1:3] == ('finalized', 'done') and row[4] and row[6] for row in case.ledger())
    with closing(sqlite3.connect(case.index)) as db, db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='ingest_state'").fetchone()
        assert db.execute('SELECT COUNT(*) FROM files').fetchone()[0] == 3
    with closing(sqlite3.connect(case.user)) as db, db:
        assert db.execute('SELECT value FROM preserved_notes').fetchone()[0] == 'authored original note'
        assert db.execute('PRAGMA user_version').fetchone()[0] == 91
    assert case.originals() == originals


def test_legacy_ingest_table_remains_preserved_when_jobs_is_selected(ingest):
    case = ingest
    # Start with an ordinary legacy index, retaining its original ledger table.
    legacy = case.base / 'legacy.sqlite'
    with closing(pipeline.connect_sqlite_live(legacy)) as db:
        pipeline.init_sqlite(db)
    case.index = legacy
    prepare(case)
    media = str(case.media / '0000.png')
    with closing(sqlite3.connect(legacy)) as db, db:
        db.execute("INSERT INTO ingest_state(media_path,phase,status,error) VALUES(?,'metadata','no_match','preserved legacy')", (media,))
    assert pipeline.run_import_finalize(case.args('import-finalize')) == 0
    assert all(row[1:3] == ('finalized', 'done') for row in case.ledger())
    with closing(sqlite3.connect(legacy)) as db, db:
        assert db.execute('SELECT phase,status,error FROM ingest_state').fetchall() == [('metadata','no_match','preserved legacy')]


def test_finalize_preserves_incomplete_outcomes_and_scoped_roots(ingest):
    case = ingest; prepare(case)
    outside = case.base / 'Unselected' / 'outside.png'; outside.parent.mkdir(); Image.new('RGB', (3, 4)).save(outside)
    with closing(sqlite3.connect(case.jobs)) as db, db:
        for number, status in enumerate(['partial', 'error', 'no_match']):
            db.execute("UPDATE ingest_state SET phase='metadata',status=?,error='retained detail',metadata_at='saved stamp' WHERE media_path=?",
                       (status, str(case.media / f'{number:04}.png')))
        db.execute("INSERT INTO ingest_state(media_path,phase,status,error) VALUES(?,'enrich','error','outside original')", (str(outside),))
    assert pipeline.run_import_finalize(case.args('import-finalize', '--report-incomplete')) == 3
    rows = case.ledger(); scoped = [row for row in rows if row[0] != str(outside)]
    assert [row[2] for row in scoped] == ['partial','error','no_match']
    assert all(row[1] == 'metadata' and row[3] == 'retained detail' and row[5] == 'saved stamp' and row[6] for row in scoped)
    assert next(row for row in rows if row[0] == str(outside))[1:] == ('enrich','error','outside original',None,None,None)
    with closing(sqlite3.connect(case.jobs)) as db, db:
        db.execute("UPDATE ingest_state SET status='done',error=NULL WHERE status IN ('partial','error') AND media_path<>?", (str(outside),))
    assert pipeline.run_import_finalize(case.args('import-finalize', '--report-incomplete')) == 0


def test_discovery_file_reads_and_reported_progress_hold_no_writer(ingest, monkeypatch):
    case = ingest; case.seed(205); original_read = pipeline.local_downloaded_at; original_print = builtins.print
    reads = []; progress = []
    def inspected(path):
        # The first call happens before the jobs store has been initialized.
        if case.jobs.exists(): writer_available(case)
        reads.append(str(path)); return original_read(path)
    def printed(*values, **kwargs):
        if values and str(values[0]).startswith('PROGRESS:'):
            done = int(str(values[0]).split(':')[1].split('/')[0])
            if done:
                writer_available(case)
                with closing(sqlite3.connect(case.jobs)) as db, db:
                    assert db.execute('SELECT COUNT(*) FROM ingest_state').fetchone()[0] == done
                progress.append(done)
        return original_print(*values, **kwargs)
    monkeypatch.setattr(pipeline, 'local_downloaded_at', inspected)
    monkeypatch.setattr(pipeline, 'print', printed, raising=False)
    assert pipeline.run_import_discover(case.args('import-discover')) == 0
    assert len(reads) == 205 and progress == [100, 200, 205]


def test_slow_enrichment_allows_real_parent_progress_persistence(ingest, monkeypatch):
    case = ingest; case.seed(101)
    assert pipeline.run_import_discover(case.args('import-discover')) == 0
    gate = threading.Event(); progress = threading.Event(); entered = threading.Event(); failures = []
    original_read = pipeline._enrich_file; original_print = builtins.print
    task = {'status':'running','progress':0,'total':101,'stage_index':1,'stage_total':1,'stage':'Reading file information'}
    identity, token = operational_state.begin('generated-ingest-progress', [['local']], task)
    def read(path):
        if Path(path).name == '0050.png':
            entered.set()
            if not gate.wait(15): raise AssertionError('Test file-read gate timed out')
        return original_read(path)
    def printed(*values, **kwargs):
        if values and values[0] == 'PROGRESS:50/101': progress.set()
        return original_print(*values, **kwargs)
    def enrich():
        try: assert pipeline.run_import_enrich(case.args('import-enrich', '--workers', '1')) == 0
        except BaseException as error: failures.append(error)
    monkeypatch.setattr(pipeline, '_enrich_file', read)
    monkeypatch.setattr(pipeline, 'print', printed, raising=False)
    worker = threading.Thread(target=enrich); worker.start()
    try:
        assert entered.wait(10) and progress.wait(10)
        task['progress'] = 50
        operational_state.persist(identity, token, task)
        assert operational_state.snapshot('generated-ingest-progress')['progress'] == 50
        assert sum(row[1] == 'enriched' for row in case.ledger()) == 50
    finally:
        gate.set(); worker.join(20)
    assert not worker.is_alive() and not failures
    assert all(row[1] == 'enriched' for row in case.ledger())
    task.update(status='done', progress=101); operational_state.persist(identity, token, task)


def test_finalize_pages_close_read_snapshots_before_other_writers(ingest, monkeypatch):
    case = ingest; case.seed(205); prepare(case)
    with closing(sqlite3.connect(case.jobs)) as db, db:
        db.execute('CREATE TABLE generated_progress(value INTEGER)'); db.execute('INSERT INTO generated_progress VALUES(0)')
    original = pipeline.existing_metadata_path_for; inspected = []
    def inspect(path, *args, **kwargs):
        with closing(sqlite3.connect(case.jobs, timeout=.2)) as db, db:
            db.execute('UPDATE generated_progress SET value=value+1')
        inspected.append(str(path)); return original(path, *args, **kwargs)
    monkeypatch.setattr(pipeline, 'existing_metadata_path_for', inspect)
    assert pipeline.run_import_finalize(case.args('import-finalize')) == 0
    assert len(inspected) == 205 and len(set(inspected)) == 205
    assert all(row[1:3] == ('finalized','done') for row in case.ledger())


def test_metadata_outcome_is_committed_before_its_progress_is_emitted(ingest, monkeypatch):
    case = ingest; prepare(case); original_print = builtins.print; outcomes = []
    def printed(*values, **kwargs):
        if values and str(values[0]).startswith('FILE_STATUS:'):
            event = json.loads(str(values[0]).split(':', 1)[1])
            if event['status'] == 'no_match':
                writer_available(case)
                with closing(sqlite3.connect(case.jobs)) as db, db:
                    assert db.execute('SELECT status FROM ingest_state WHERE media_path=?', (event['path'],)).fetchone()[0] == 'no_match'
                outcomes.append(event['path'])
        return original_print(*values, **kwargs)
    monkeypatch.setattr(pipeline, 'print', printed, raising=False)
    monkeypatch.setattr(pipeline, 'find_post_by_md5', lambda *args, **kwargs: (None, None, None))
    args = pipeline.build_parser().parse_args([
        '--root', str(case.media), '--sidecar-dir', str(case.base / 'sidecars'), '--jobs-db', str(case.jobs),
        '--user-db', str(case.user), 'backfill', '--use-indexed-md5', '--database', str(case.index), '--delay', '0', str(case.media)])
    assert pipeline.run_backfill(args) == 0 and len(outcomes) == 3
    assert pipeline.run_import_finalize(case.args('import-finalize')) == 0
    assert all(row[2] == 'no_match' for row in case.ledger())


def test_real_tool_child_completes_all_local_split_store_phases(ingest, monkeypatch):
    case = ingest; originals = case.originals(); identity = 'ingest-proof-' + uuid4().hex
    monkeypatch.setattr(tools, 'JOBS_DB_PATH', case.jobs)
    monkeypatch.setattr(tools, 'TOOL_WORKING_DIRECTORY', case.base)
    commands = []
    for phase in ['import-discover','import-enrich','import-finalize']:
        commands.append([sys.executable, str(ROOT / 'scripts/danbooru_gallery_dl.py'),
            '--root', str(case.media), '--sidecar-dir', str(case.base / 'sidecars'),
            '--jobs-db', str(case.jobs), '--user-db', str(case.user), phase, '--output', str(case.index), str(case.media)])
    assert tools._launch_tool(identity, commands, environment={**os.environ, 'KEIVOTOS_HOME':str(case.base / 'child-profile')},
        stage_names=['Finding files','Reading file information','Updating library'])['status'] == 'started'
    deadline = time.monotonic() + 45
    while identity in tools._running_threads and time.monotonic() < deadline: time.sleep(.02)
    assert identity not in tools._running_threads
    task = tools.tool_task_snapshot(identity)
    assert task['status'] == 'done', task
    assert task['stage_index'] == 3 and task['result_counts']['error'] == 0
    assert operational_state.snapshot(identity)['status'] == 'done'
    assert all(row[1:3] == ('finalized','done') for row in case.ledger())
    assert case.originals() == originals
    tools._running_tasks.pop(identity, None)

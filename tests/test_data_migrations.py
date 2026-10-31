"""Verified SQLite snapshot behavior, including actual concurrent WAL writes."""
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import hashlib
import json
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from services import data_migrations as migrations
from files_base.sources import FILES_SOURCES_SCHEMA
from files_base.serving import ServeDenied
from modules.youtube import SCHEMA as YOUTUBE_SCHEMA
from modules.youtube.user_schema import SCHEMA as YOUTUBE_PERSONAL_SCHEMA, import_catalog_identities, import_catalog_captures, register_catalog_assets
from modules.youtube import metadata as youtube_metadata


def make_source(path):
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript('CREATE TABLE personal(id INTEGER PRIMARY KEY AUTOINCREMENT,note TEXT,payload BLOB,value REAL);'
                                 'CREATE INDEX personal_note ON personal(note); CREATE VIEW kept AS SELECT * FROM personal;')
        connection.executemany('INSERT INTO personal(note,payload,value) VALUES(?,?,?)',
                               [('한국어', b'\0\xff', 1.25), ('duplicate', None, None), ('duplicate', None, None)])
        connection.execute('DELETE FROM personal WHERE id=2')
        connection.commit()


def test_snapshot_preserves_schema_values_sequences_and_source_bytes(tmp_path):
    source = tmp_path / ('user #.sqlite' if os.name == 'nt' else 'user ?#.sqlite')
    target = tmp_path / 'candidate.sqlite'
    make_source(source)
    original = source.read_bytes()
    result = migrations.snapshot_user_database(source, target)
    assert source.read_bytes() == original
    assert result.destination == target
    assert result.table_rows == {'personal': 2, 'sqlite_sequence': 1}
    with closing(sqlite3.connect(source)) as old, closing(sqlite3.connect(target)) as new:
        assert old.execute('SELECT * FROM personal').fetchall() == new.execute('SELECT * FROM personal').fetchall()
        assert new.execute('SELECT seq FROM sqlite_sequence').fetchone()[0] == 3
        assert old.execute('SELECT * FROM sqlite_master').fetchall() == new.execute('SELECT * FROM sqlite_master').fetchall()
    assert list(tmp_path.glob('*.partial')) == []


def test_existing_destination_is_never_replaced(tmp_path):
    source, target = tmp_path / 'user.sqlite', tmp_path / 'candidate.sqlite'
    make_source(source)
    target.write_bytes(b'previous preserved snapshot')
    with pytest.raises(FileExistsError):
        migrations.snapshot_user_database(source, target)
    assert target.read_bytes() == b'previous preserved snapshot'
    assert not list(tmp_path.glob('*.partial'))


def test_failed_validation_retains_candidate_without_publishing(tmp_path, monkeypatch):
    source, target = tmp_path / 'user.sqlite', tmp_path / 'candidate.sqlite'
    make_source(source)
    original = source.read_bytes()
    manifest = migrations._manifest
    calls = 0
    def mismatch(connection):
        nonlocal calls
        calls += 1
        result = manifest(connection)
        return ('different', result[1], result[2]) if calls == 2 else result
    monkeypatch.setattr(migrations, '_manifest', mismatch)
    with pytest.raises(migrations.SnapshotError) as error:
        migrations.snapshot_user_database(source, target)
    assert not target.exists()
    assert error.value.staging_path.is_file()
    assert source.read_bytes() == original
    with closing(sqlite3.connect(error.value.staging_path)) as retained:
        assert retained.execute('SELECT count(*) FROM personal').fetchone()[0] == 2


def test_destination_race_preserves_winner_and_verified_candidate(tmp_path, monkeypatch):
    source, target = tmp_path / 'user.sqlite', tmp_path / 'candidate.sqlite'
    make_source(source)
    link = migrations.os.link
    def race(candidate, destination):
        Path(destination).write_bytes(b'existing winner')
        link(candidate, destination)
    monkeypatch.setattr(migrations.os, 'link', race)
    with pytest.raises(migrations.SnapshotError) as error:
        migrations.snapshot_user_database(source, target)
    assert target.read_bytes() == b'existing winner'
    assert error.value.staging_path.exists()


def test_backup_uses_one_snapshot_despite_concurrent_wal_commit(tmp_path, monkeypatch):
    source, target = tmp_path / 'user.sqlite', tmp_path / 'candidate.sqlite'
    make_source(source)
    with closing(sqlite3.connect(source)) as writer:
        assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        manifest = migrations._manifest
        calls = 0
        def with_concurrent_commit(connection):
            nonlocal calls
            calls += 1
            result = manifest(connection)
            if calls == 1:
                writer.execute("INSERT INTO personal(note) VALUES('committed during backup')")
                writer.commit()
            return result
        monkeypatch.setattr(migrations, '_manifest', with_concurrent_commit)
        result = migrations.snapshot_user_database(source, target)
        assert writer.execute('SELECT count(*) FROM personal').fetchone()[0] == 3
    assert result.table_rows['personal'] == 2
    with closing(sqlite3.connect(target)) as copied:
        assert copied.execute('SELECT count(*) FROM personal').fetchone()[0] == 2


def test_corrupt_source_is_not_activated_or_overwritten(tmp_path):
    source, target = tmp_path / 'user.sqlite', tmp_path / 'candidate.sqlite'
    source.write_bytes(b'not a database')
    with pytest.raises(migrations.SnapshotError) as error:
        migrations.snapshot_user_database(source, target)
    assert error.value.staging_path.exists()
    assert source.read_bytes() == b'not a database'
    assert not target.exists()


def test_snapshot_handles_quoted_table_and_column_identifiers(tmp_path):
    source, target = tmp_path / 'user.sqlite', tmp_path / 'candidate.sqlite'
    with closing(sqlite3.connect(source)) as connection:
        connection.execute('CREATE TABLE "a""b"("x""y" TEXT)')
        connection.execute('INSERT INTO "a""b" VALUES(?)', ('safe',))
        connection.commit()
    result = migrations.snapshot_user_database(source, target)
    assert result.table_rows['a"b'] == 1


def test_missing_source_is_not_created(tmp_path):
    source, target = tmp_path / 'missing.sqlite', tmp_path / 'candidate.sqlite'
    with pytest.raises(FileNotFoundError):
        migrations.snapshot_user_database(source, target)
    assert not source.exists() and not target.exists()


def test_flush_failure_retains_candidate_and_never_publishes(tmp_path, monkeypatch):
    source, target = tmp_path / 'user.sqlite', tmp_path / 'candidate.sqlite'
    make_source(source)
    original = source.read_bytes()
    def unavailable(_descriptor):
        raise OSError('disk flush failed')
    monkeypatch.setattr(migrations.os, 'fsync', unavailable)
    with pytest.raises(migrations.SnapshotError) as error:
        migrations.snapshot_user_database(source, target)
    assert error.value.staging_path.exists()
    assert not target.exists()
    assert source.read_bytes() == original


@pytest.fixture
def essential_source(tmp_path):
    source = tmp_path / 'user.sqlite'
    make_source(source)
    with closing(sqlite3.connect(source)) as connection:
        connection.executescript(FILES_SOURCES_SCHEMA + """
            CREATE TABLE saved_progress(item TEXT PRIMARY KEY, position INTEGER);
            INSERT INTO saved_progress VALUES('kept episode',123);
            CREATE TABLE saved_jobs(id TEXT PRIMARY KEY,status TEXT,path TEXT);
            INSERT INTO saved_jobs VALUES('job-old','complete','video.webm');
            CREATE TABLE saved_playlist(job_id TEXT PRIMARY KEY REFERENCES saved_jobs(id));
            INSERT INTO saved_playlist VALUES('job-old');
            PRAGMA user_version=17;
        """)
        connection.executemany('INSERT INTO files_sources(source_id,path,display_name,role,visible) VALUES(?,?,?,?,?)',
            [('src-first', str(tmp_path / 'unavailable-one'), '한국어', 'youtube', 1),
             ('src-hidden', str(tmp_path / 'unavailable-two'), 'Hidden library', 'files', 0)])
        connection.commit()
    return source


def test_preparation_preserves_all_legacy_records_and_source_ids(essential_source, tmp_path):
    source = essential_source
    original = source.read_bytes()
    with closing(sqlite3.connect(source)) as connection:
        before = migrations._manifest(connection)
        objects = {(row[0], row[1]) for row in connection.execute('SELECT type,name FROM sqlite_master WHERE sql IS NOT NULL')}
    prepared = migrations.prepare_essential_candidate(source, tmp_path / 'attempt')
    assert prepared.registered_libraries == 2
    assert prepared.legacy_table_rows == before[2]
    assert source.read_bytes() == original
    with closing(sqlite3.connect(prepared.candidate)) as candidate:
        assert migrations._manifest(candidate, schema_objects=objects, table_names=set(before[2])) == before
        assert candidate.execute('PRAGMA user_version').fetchone()[0] == 17
        assert candidate.execute('SELECT id,kind,display_name FROM kv_libraries ORDER BY id').fetchall() == [
            ('src-first', 'linked', '한국어'), ('src-hidden', 'linked', 'Hidden library')]
        assert candidate.execute('SELECT legacy_key,library_id FROM kv_library_sources ORDER BY legacy_key').fetchall() == [
            ('src-first', 'src-first'), ('src-hidden', 'src-hidden')]
    assert migrations.validate_essential_candidate(source, prepared.attempt_directory) == prepared
    # These paths were metadata only; preparation never scans or creates media.
    assert not (tmp_path / 'unavailable-one').exists()
    assert not (tmp_path / 'unavailable-two').exists()


def test_preparation_retry_keeps_exact_candidate_and_phase_receipts(essential_source, tmp_path):
    attempt = tmp_path / 'attempt'
    first = migrations.prepare_essential_candidate(essential_source, attempt)
    paths = [attempt / (phase + '.json') for phase in ('intent', 'snapshot', 'prepared')] + [first.candidate]
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    assert migrations.prepare_essential_candidate(essential_source, attempt) == first
    assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths} == before


@pytest.mark.parametrize('phase', ['intent', 'snapshot', 'prepared'])
def test_interruption_after_each_phase_is_resumable(essential_source, tmp_path, monkeypatch, phase):
    attempt = tmp_path / 'attempt'
    original = migrations._record_phase
    def interrupted(directory, current, **facts):
        original(directory, current, **facts)
        if current == phase:
            raise RuntimeError('simulated interrupted process')
    monkeypatch.setattr(migrations, '_record_phase', interrupted)
    with pytest.raises(migrations.PreparationError):
        migrations.prepare_essential_candidate(essential_source, attempt)
    saved = (attempt / (phase + '.json')).read_bytes()
    monkeypatch.setattr(migrations, '_record_phase', original)
    prepared = migrations.prepare_essential_candidate(essential_source, attempt)
    assert prepared.registered_libraries == 2
    assert (attempt / (phase + '.json')).read_bytes() == saved
    assert migrations.validate_essential_candidate(essential_source, attempt) == prepared


def test_interruption_between_copy_and_receipt_recovers_only_matching_copy(essential_source, tmp_path, monkeypatch):
    attempt = tmp_path / 'attempt'
    original = migrations.snapshot_user_database
    def copied_then_interrupted(*arguments):
        original(*arguments)
        raise RuntimeError('crash before snapshot receipt')
    monkeypatch.setattr(migrations, 'snapshot_user_database', copied_then_interrupted)
    with pytest.raises(migrations.PreparationError):
        migrations.prepare_essential_candidate(essential_source, attempt)
    assert (attempt / 'essential.sqlite').exists() and not (attempt / 'snapshot.json').exists()
    monkeypatch.setattr(migrations, 'snapshot_user_database', original)
    assert migrations.prepare_essential_candidate(essential_source, attempt).registered_libraries == 2


def test_partial_catalog_creation_can_resume(essential_source, tmp_path, monkeypatch):
    attempt = tmp_path / 'attempt'
    original = migrations.item_catalog.ensure_catalog_schema
    def interrupted(connection):
        original(connection)
        raise RuntimeError('catalog created before crash')
    monkeypatch.setattr(migrations.item_catalog, 'ensure_catalog_schema', interrupted)
    with pytest.raises(migrations.PreparationError):
        migrations.prepare_essential_candidate(essential_source, attempt)
    monkeypatch.setattr(migrations.item_catalog, 'ensure_catalog_schema', original)
    assert migrations.prepare_essential_candidate(essential_source, attempt).registered_libraries == 2


def test_import_failure_rolls_back_entire_alias_batch(essential_source, tmp_path, monkeypatch):
    attempt = tmp_path / 'attempt'
    original = migrations.item_catalog.ensure_library
    def interrupted(connection, **values):
        identity = original(connection, **values)
        if identity == 'src-hidden':
            raise RuntimeError('interrupted alias import')
        return identity
    monkeypatch.setattr(migrations.item_catalog, 'ensure_library', interrupted)
    with pytest.raises(migrations.PreparationError):
        migrations.prepare_essential_candidate(essential_source, attempt)
    with closing(sqlite3.connect(attempt / 'essential.sqlite')) as candidate:
        assert candidate.execute('SELECT count(*) FROM kv_libraries').fetchone()[0] == 0
        assert candidate.execute('SELECT count(*) FROM kv_library_sources').fetchone()[0] == 0
        assert candidate.execute('SELECT count(*) FROM files_sources').fetchone()[0] == 2
    monkeypatch.setattr(migrations.item_catalog, 'ensure_library', original)
    assert migrations.prepare_essential_candidate(essential_source, attempt).registered_libraries == 2


def test_changed_source_refuses_stale_prepared_candidate(essential_source, tmp_path):
    attempt = tmp_path / 'attempt'
    prepared = migrations.prepare_essential_candidate(essential_source, attempt)
    saved = prepared.candidate.read_bytes()
    with closing(sqlite3.connect(essential_source)) as source:
        source.execute("UPDATE saved_progress SET position=456")
        source.commit()
    for action in (migrations.validate_essential_candidate, migrations.prepare_essential_candidate):
        with pytest.raises(migrations.PreparationError, match='Source changed'):
            action(essential_source, attempt)
    assert prepared.candidate.read_bytes() == saved
    replacement = migrations.prepare_essential_candidate(essential_source, tmp_path / 'new-attempt')
    with closing(sqlite3.connect(replacement.candidate)) as candidate:
        assert candidate.execute('SELECT position FROM saved_progress').fetchone()[0] == 456
    assert prepared.candidate.read_bytes() == saved


def test_changed_candidate_is_refused_without_replacement(essential_source, tmp_path):
    prepared = migrations.prepare_essential_candidate(essential_source, tmp_path / 'attempt')
    with closing(sqlite3.connect(prepared.candidate)) as candidate:
        candidate.execute("UPDATE personal SET note='external candidate edit' WHERE id=1")
        candidate.commit()
    saved = prepared.candidate.read_bytes()
    with pytest.raises(migrations.PreparationError, match='Prepared candidate changed'):
        migrations.prepare_essential_candidate(essential_source, prepared.attempt_directory)
    assert prepared.candidate.read_bytes() == saved


@pytest.mark.parametrize('pragma', ['user_version', 'application_id'])
@pytest.mark.parametrize('changed_database', ['source', 'candidate'])
def test_header_only_revision_change_is_refused(essential_source, tmp_path, pragma, changed_database):
    prepared = migrations.prepare_essential_candidate(essential_source, tmp_path / 'attempt')
    changed = essential_source if changed_database == 'source' else prepared.candidate
    with closing(sqlite3.connect(changed)) as connection:
        connection.execute('PRAGMA ' + pragma + '=99')
    saved = prepared.candidate.read_bytes()
    message = 'Source changed' if changed_database == 'source' else 'Prepared candidate changed'
    with pytest.raises(migrations.PreparationError, match=message):
        migrations.validate_essential_candidate(essential_source, prepared.attempt_directory)
    assert prepared.candidate.read_bytes() == saved


def test_unrelated_schema_added_during_interruption_is_not_accepted(essential_source, tmp_path, monkeypatch):
    attempt = tmp_path / 'attempt'
    original = migrations._record_phase
    def interrupted(directory, phase, **facts):
        original(directory, phase, **facts)
        if phase == 'snapshot':
            raise RuntimeError('interrupted')
    monkeypatch.setattr(migrations, '_record_phase', interrupted)
    with pytest.raises(migrations.PreparationError):
        migrations.prepare_essential_candidate(essential_source, attempt)
    with closing(sqlite3.connect(attempt / 'essential.sqlite')) as candidate:
        candidate.execute('CREATE TABLE unrelated(note TEXT)')
        candidate.commit()
    monkeypatch.setattr(migrations, '_record_phase', original)
    with pytest.raises(migrations.PreparationError, match='Unexpected schema changes'):
        migrations.prepare_essential_candidate(essential_source, attempt)
    with closing(sqlite3.connect(attempt / 'essential.sqlite')) as candidate:
        assert not candidate.execute("SELECT 1 FROM sqlite_master WHERE name='kv_catalog_meta'").fetchone()


@pytest.mark.parametrize('phase', ['intent', 'snapshot', 'prepared'])
def test_unknown_phase_format_is_not_rewritten(essential_source, tmp_path, phase):
    attempt = tmp_path / 'attempt'
    prepared = migrations.prepare_essential_candidate(essential_source, attempt)
    receipt = attempt / (phase + '.json')
    data = json.loads(receipt.read_text(encoding='utf-8'))
    data['format'] = 99
    receipt.write_text(json.dumps(data), encoding='utf-8')
    saved, candidate = receipt.read_bytes(), prepared.candidate.read_bytes()
    with pytest.raises(migrations.PreparationError, match='Unsupported preparation'):
        migrations.prepare_essential_candidate(essential_source, attempt)
    assert receipt.read_bytes() == saved and prepared.candidate.read_bytes() == candidate


def test_unknown_existing_directory_and_other_origin_are_preserved(essential_source, tmp_path):
    unknown = tmp_path / 'unknown'
    unknown.mkdir()
    (unknown / 'note.txt').write_text('keep me')
    with pytest.raises(migrations.PreparationError, match='Cannot read intent'):
        migrations.prepare_essential_candidate(essential_source, unknown)
    assert sorted(path.name for path in unknown.iterdir()) == ['note.txt']
    attempt = tmp_path / 'attempt'
    migrations.prepare_essential_candidate(essential_source, attempt)
    other = tmp_path / 'other.sqlite'
    make_source(other)
    with pytest.raises(migrations.PreparationError, match='different source'):
        migrations.prepare_essential_candidate(other, attempt)


def test_attempt_lock_excludes_another_process_and_releases_on_exit(essential_source, tmp_path):
    attempt = tmp_path / 'attempt'
    migrations.prepare_essential_candidate(essential_source, attempt)
    backend = str(Path(__file__).resolve().parents[1] / 'backend')
    code = """import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from services import data_migrations as m
try:
    with m._attempt_lock(Path(sys.argv[2])):
        os._exit(0)
except m.PreparationError:
    sys.exit(17)
"""
    options = dict(capture_output=True, timeout=20)
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
    with migrations._attempt_lock(attempt):
        result = subprocess.run([sys.executable, '-c', code, backend, str(attempt)], **options)
        assert result.returncode == 17, result.stderr
    result = subprocess.run([sys.executable, '-c', code, backend, str(attempt)], **options)
    assert result.returncode == 0, result.stderr
    assert migrations.validate_essential_candidate(essential_source, attempt).registered_libraries == 2


@pytest.fixture
def youtube_source(essential_source):
    with closing(sqlite3.connect(essential_source)) as connection:
        connection.executescript(YOUTUBE_SCHEMA + YOUTUBE_PERSONAL_SCHEMA)
        connection.executemany('''INSERT INTO youtube_downloads
            (id,url,source_id,root,video,audio,status,title,path) VALUES(?,?,?,?,?,?,?,?,?)''',
            [(identity, 'https://www.youtube.com/watch?v=abcdefghijk', 'src-first', '/unavailable/old-root',
              'best', 'best', status, 'Same title', 'same/path.mp4')
             for identity, status in [('one', 'complete'), ('two', 'complete'), ('failed', 'failed'), ('queued', 'queued')]])
        connection.execute("INSERT INTO youtube_playlists(id,request_id,title) VALUES('playlist','request','Preserved playlist')")
        connection.executemany("INSERT INTO youtube_playlist_items(playlist_id,job_id,position) VALUES('playlist',?,?)",
                               [('two', 0), ('one', 1), ('missing-history', 2)])
        connection.commit()
    return essential_source


def youtube_importer():
    return migrations.CatalogImporter('youtube.saved-media', 1, import_catalog_identities)


def test_owner_import_preserves_legacy_and_does_not_merge_duplicate_media(youtube_source, tmp_path):
    original = youtube_source.read_bytes()
    with closing(sqlite3.connect(youtube_source)) as source:
        before = migrations._state(source)
        objects = {(row[0], row[1]) for row in source.execute('SELECT type,name FROM sqlite_master WHERE sql IS NOT NULL')}
    importer = youtube_importer()
    prepared = migrations.prepare_essential_candidate(youtube_source, tmp_path / 'attempt', catalog_importers=[importer])
    assert prepared.imported_items == {'youtube.saved-media': 2}
    with closing(sqlite3.connect(prepared.candidate)) as candidate:
        assert migrations._state(candidate, schema_objects=objects, table_names=set(before['table_rows'])) == before
        rows = candidate.execute('SELECT legacy_key,item_id FROM kv_legacy_keys ORDER BY legacy_key').fetchall()
        assert [row[0] for row in rows] == ['one', 'two']
        assert len({row[1] for row in rows}) == 2
        assert candidate.execute('SELECT DISTINCT owner,item_type FROM kv_items').fetchall() == [('youtube', 'saved-media')]
        assert candidate.execute("SELECT job_id,position FROM youtube_playlist_items ORDER BY position").fetchall() == [
            ('two', 0), ('one', 1), ('missing-history', 2)]
    assert migrations.validate_essential_candidate(youtube_source, prepared.attempt_directory, catalog_importers=[importer]) == prepared
    assert youtube_source.read_bytes() == original


def test_imported_ids_survive_new_attempts_title_and_location_changes(youtube_source, tmp_path):
    importer = youtube_importer()
    first = migrations.prepare_essential_candidate(youtube_source, tmp_path / 'first', catalog_importers=[importer])
    with closing(sqlite3.connect(first.candidate)) as connection:
        identities = connection.execute('SELECT * FROM kv_legacy_keys ORDER BY legacy_key').fetchall()
    saved = first.candidate.read_bytes()
    with closing(sqlite3.connect(youtube_source)) as connection:
        connection.execute("UPDATE youtube_downloads SET title='Renamed', root='Z:/relocated',path='different/video.webm'")
        connection.commit()
    second = migrations.prepare_essential_candidate(youtube_source, tmp_path / 'second', catalog_importers=[importer])
    with closing(sqlite3.connect(second.candidate)) as connection:
        assert connection.execute('SELECT * FROM kv_legacy_keys ORDER BY legacy_key').fetchall() == identities
    assert first.candidate.read_bytes() == saved


def test_importer_configuration_is_recorded_and_exact_on_retry(youtube_source, tmp_path):
    attempt = tmp_path / 'attempt'
    importer = youtube_importer()
    first = migrations.prepare_essential_candidate(youtube_source, attempt, catalog_importers=[importer])
    paths = [first.candidate, attempt / 'intent.json', attempt / 'snapshot.json', attempt / 'prepared.json']
    before = {path: path.read_bytes() for path in paths}
    assert json.loads((attempt / 'intent.json').read_text())['catalog_importers'] == [dict(name=importer.name, version=1)]
    assert migrations.prepare_essential_candidate(youtube_source, attempt, catalog_importers=[importer]) == first
    for configuration in ([], [migrations.CatalogImporter(importer.name, 2, import_catalog_identities)]):
        for action in (migrations.prepare_essential_candidate, migrations.validate_essential_candidate):
            with pytest.raises(migrations.PreparationError, match='configuration changed'):
                action(youtube_source, attempt, catalog_importers=configuration)
    assert {path: path.read_bytes() for path in paths} == before


@pytest.mark.parametrize('value', [True, 1.0])
def test_receipt_importer_version_requires_integer(youtube_source, tmp_path, value):
    importer = youtube_importer()
    attempt = tmp_path / 'attempt'
    migrations.prepare_essential_candidate(youtube_source, attempt, catalog_importers=[importer])
    path = attempt / 'intent.json'
    intent = json.loads(path.read_text())
    intent['catalog_importers'][0]['version'] = value
    path.write_text(json.dumps(intent))
    with pytest.raises(migrations.PreparationError, match='configuration changed'):
        migrations.validate_essential_candidate(youtube_source, attempt, catalog_importers=[importer])


def test_owner_failure_rolls_back_libraries_and_items_then_resumes(youtube_source, tmp_path):
    attempt = tmp_path / 'attempt'
    def interrupted(connection):
        import_catalog_identities(connection)
        raise RuntimeError('interrupted after owner inserts')
    failing = migrations.CatalogImporter('youtube.saved-media', 1, interrupted)
    with pytest.raises(migrations.PreparationError, match='interrupted after owner inserts'):
        migrations.prepare_essential_candidate(youtube_source, attempt, catalog_importers=[failing])
    with closing(sqlite3.connect(attempt / 'essential.sqlite')) as candidate:
        for table in ('kv_libraries', 'kv_library_sources', 'kv_items', 'kv_legacy_keys'):
            assert candidate.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0
        assert candidate.execute("SELECT count(*) FROM youtube_downloads WHERE status='complete'").fetchone()[0] == 2
    prepared = migrations.prepare_essential_candidate(youtube_source, attempt, catalog_importers=[youtube_importer()])
    assert prepared.imported_items == {'youtube.saved-media': 2}


def test_owner_cannot_change_legacy_records_in_successful_preparation(essential_source, tmp_path):
    def changed(connection):
        connection.execute('UPDATE saved_progress SET position=999')
        return 0
    importer = migrations.CatalogImporter('test.changed', 1, changed)
    with pytest.raises(migrations.PreparationError, match='changed legacy records'):
        migrations.prepare_essential_candidate(essential_source, tmp_path / 'attempt', catalog_importers=[importer])
    with closing(sqlite3.connect(tmp_path / 'attempt/essential.sqlite')) as candidate:
        assert candidate.execute('SELECT position FROM saved_progress').fetchone()[0] == 123
        assert candidate.execute('SELECT count(*) FROM kv_libraries').fetchone()[0] == 0


def test_pre_importer_receipts_remain_compatible(essential_source, tmp_path):
    attempt = tmp_path / 'attempt'
    first = migrations.prepare_essential_candidate(essential_source, attempt)
    for phase, field in [('intent', 'catalog_importers'), ('prepared', 'imported_items')]:
        path = attempt / (phase + '.json')
        receipt = json.loads(path.read_text())
        del receipt[field]
        path.write_text(json.dumps(receipt))
    assert migrations.prepare_essential_candidate(essential_source, attempt) == first


@pytest.mark.parametrize('configuration', [
    [object()], [migrations.CatalogImporter('wrong name', 1, lambda c: 0)],
    [migrations.CatalogImporter('youtube.saved-media', True, lambda c: 0)],
    [migrations.CatalogImporter('youtube.saved-media', 0, lambda c: 0)],
    [migrations.CatalogImporter('youtube.saved-media', 1, None)],
    [youtube_importer(), youtube_importer()]])
def test_invalid_importer_configuration_creates_no_attempt(essential_source, tmp_path, configuration):
    attempt = tmp_path / 'attempt'
    with pytest.raises(ValueError, match='catalog importer'):
        migrations.prepare_essential_candidate(essential_source, attempt, catalog_importers=configuration)
    assert not attempt.exists()


def test_absent_owner_table_and_dict_factory_are_supported(essential_source, youtube_source, tmp_path):
    # Use a separate minimal database because youtube_source adds to essential_source.
    absent = tmp_path / 'without-youtube.sqlite'
    make_source(absent)
    result = migrations.prepare_essential_candidate(absent, tmp_path / 'absent', catalog_importers=[youtube_importer()])
    assert result.imported_items == {'youtube.saved-media': 0}
    with closing(sqlite3.connect(youtube_source)) as connection:
        migrations.item_catalog.ensure_catalog_schema(connection)
        factory = lambda cursor, row: dict(zip([column[0] for column in cursor.description], row))
        connection.row_factory = factory
        connection.execute('BEGIN IMMEDIATE')
        assert import_catalog_identities(connection) == 2
        assert import_catalog_identities(connection) == 2
        assert connection.in_transaction and connection.row_factory is factory
        assert connection.execute('SELECT count(*) AS n FROM kv_items').fetchone()['n'] == 2
        connection.rollback()


def test_importer_order_is_canonical(essential_source, tmp_path):
    first = migrations.CatalogImporter('first.owner', 1, lambda connection: 0)
    second = migrations.CatalogImporter('second.owner', 1, lambda connection: 0)
    result = migrations.prepare_essential_candidate(essential_source, tmp_path / 'attempt', catalog_importers=[second, first])
    assert result.imported_items == {'first.owner': 0, 'second.owner': 0}
    assert migrations.prepare_essential_candidate(essential_source, result.attempt_directory, catalog_importers=[first, second]) == result


@pytest.mark.parametrize('bad_count', [None, -1, True, 1.0])
def test_invalid_import_count_rolls_back_every_alias(essential_source, tmp_path, bad_count):
    importer = migrations.CatalogImporter('invalid.count', 1, lambda connection: bad_count)
    with pytest.raises(migrations.PreparationError, match='return a count'):
        migrations.prepare_essential_candidate(essential_source, tmp_path / 'attempt', catalog_importers=[importer])
    with closing(sqlite3.connect(tmp_path / 'attempt/essential.sqlite')) as connection:
        assert connection.execute('SELECT count(*) FROM kv_libraries').fetchone()[0] == 0


def test_conflicting_owner_alias_rolls_back_earlier_completed_item(youtube_source):
    with closing(sqlite3.connect(youtube_source)) as connection:
        migrations.item_catalog.ensure_catalog_schema(connection)
        migrations.item_catalog.ensure_item(connection, namespace='youtube.download', legacy_key='two',
                                             owner='other', item_type='capture', item_id='preserved-conflict')
        with pytest.raises(migrations.item_catalog.CatalogConflict):
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                import_catalog_identities(connection)
        assert connection.execute('SELECT legacy_key,item_id FROM kv_legacy_keys').fetchall() == [('two', 'preserved-conflict')]
        assert connection.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 1
        assert connection.execute('SELECT count(*) FROM youtube_downloads').fetchone()[0] == 4


def test_unsupported_owner_schema_is_preserved_and_caller_transaction_required(tmp_path):
    source = tmp_path / 'legacy.sqlite'
    with closing(sqlite3.connect(source)) as connection:
        connection.execute('CREATE TABLE youtube_downloads(id TEXT PRIMARY KEY)')
        connection.commit()
        with pytest.raises(ValueError, match='caller transaction'):
            import_catalog_identities(connection)
    original = source.read_bytes()
    with pytest.raises(migrations.PreparationError, match='Unsupported YouTube'):
        migrations.prepare_essential_candidate(source, tmp_path / 'attempt', catalog_importers=[youtube_importer()])
    assert source.read_bytes() == original


@pytest.fixture
def captured_youtube_source(youtube_source):
    with closing(sqlite3.connect(youtube_source)) as connection:
        for name, definition in youtube_metadata.COLUMNS.items():
            connection.execute('ALTER TABLE youtube_downloads ADD COLUMN ' + name + ' ' + definition)
        connection.executescript('''
            ALTER TABLE youtube_downloads ADD COLUMN layout_version INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE youtube_downloads ADD COLUMN bundle_path TEXT NOT NULL DEFAULT '';
            ALTER TABLE youtube_downloads ADD COLUMN manifest_sha256 TEXT NOT NULL DEFAULT '';
        ''')
        connection.execute('''UPDATE youtube_downloads SET title=?, metadata_version=1,
            video_id='abcdefghijk',channel_id=?,channel_name=?,channel_url=?,
            captured_at='2026-10-22T00:01:02Z',upload_date='20261020',duration_seconds=0,
            media_ext='webm',format_id='format-id',video_codec='vp9',audio_codec='opus',
            avatar='YouTube/channel/saved/.keivotos/channel-avatar.png',avatar_error='',
            path='YouTube/channel/saved/video.webm',thumbnail='YouTube/channel/saved/thumbnail.png',
            layout_version=1,bundle_path='YouTube/channel/saved',manifest_sha256=?,retry_of='failed'
            WHERE id='one' ''', ('한국어 saved title', 'UC' + 'a' * 22, 'Captured channel 한글',
                                'https://www.youtube.com/channel/UC' + 'a' * 22, 'ab' * 32))
        connection.execute("UPDATE youtube_downloads SET retry_of='queued' WHERE id='failed'")
        connection.commit()
    return youtube_source


def capture_importer(callback=import_catalog_captures):
    return migrations.CatalogImporter('youtube.saved-media', 2, callback)


def test_full_capture_bridge_preserves_legacy_and_known_facts(captured_youtube_source, tmp_path):
    source = captured_youtube_source
    original = source.read_bytes()
    with closing(sqlite3.connect(source)) as connection:
        before = migrations._state(connection)
        objects = {(row[0], row[1]) for row in connection.execute('SELECT type,name FROM sqlite_master WHERE sql IS NOT NULL')}
    result = migrations.prepare_essential_candidate(source, tmp_path / 'attempt', catalog_importers=[capture_importer()])
    assert result.imported_items == {'youtube.saved-media': 2}
    with closing(sqlite3.connect(result.candidate)) as connection:
        assert migrations._state(connection, schema_objects=objects, table_names=set(before['table_rows'])) == before
        captured = youtube_metadata.preserved_capture(connection, 'one')
        payload = captured['payload']
        assert payload['legacy_job_ids'] == ['one', 'failed', 'queued']
        row = payload['record']
        assert row['title'] == '한국어 saved title' and row['channel_name'] == 'Captured channel 한글'
        assert row['root'] == '/unavailable/old-root'
        assert row['duration_seconds'] == 0 and row['captured_at'] == '2026-10-22T00:01:02Z'
        assert row['avatar'] == 'YouTube/channel/saved/.keivotos/channel-avatar.png'
        assert row['manifest_sha256'] == 'ab' * 32 and row['bundle_path'] == 'YouTube/channel/saved'
        assert not set(row) & {'status', 'progress', 'error', 'superseded_by', 'failure_phase'}
        assert youtube_metadata.preserved_capture(connection, 'two')['payload']['record']['duration_seconds'] is None
        assert youtube_metadata.preserved_capture(connection, 'failed') is None
        assert youtube_metadata.preserved_capture(connection, 'missing-history') is None
        assert connection.execute('SELECT count(*) FROM kv_item_captures').fetchone()[0] == 2
    assert source.read_bytes() == original
    saved = result.candidate.read_bytes()
    assert migrations.prepare_essential_candidate(source, result.attempt_directory, catalog_importers=[capture_importer()]) == result
    assert result.candidate.read_bytes() == saved


def test_preserved_metadata_reads_from_catalog_without_download_history(captured_youtube_source, tmp_path):
    prepared = migrations.prepare_essential_candidate(captured_youtube_source, tmp_path / 'attempt', catalog_importers=[capture_importer()])
    with closing(sqlite3.connect(prepared.candidate)) as original, closing(sqlite3.connect(':memory:')) as catalog_only:
        migrations.item_catalog.ensure_catalog_schema(catalog_only)
        for table in ('kv_items', 'kv_legacy_keys', 'kv_item_captures'):
            for row in original.execute('SELECT * FROM ' + table):
                catalog_only.execute('INSERT INTO ' + table + ' VALUES(' + ','.join('?' for _ in row) + ')', row)
        catalog_only.commit()
        assert not catalog_only.execute("SELECT 1 FROM sqlite_master WHERE name='youtube_downloads'").fetchone()
        statements = []
        catalog_only.set_trace_callback(statements.append)
        changes = catalog_only.total_changes
        captured = youtube_metadata.preserved_capture(catalog_only, 'one')
        assert captured['payload']['record']['title'] == '한국어 saved title'
        assert captured['payload']['legacy_job_ids'] == ['one', 'failed', 'queued']
        assert catalog_only.total_changes == changes
        assert all(statement.lstrip().upper().startswith('SELECT') and 'youtube_downloads' not in statement for statement in statements)


def test_capture_bridge_handles_old_columns_and_absent_owner(youtube_source, tmp_path):
    prepared = migrations.prepare_essential_candidate(youtube_source, tmp_path / 'old', catalog_importers=[capture_importer()])
    with closing(sqlite3.connect(prepared.candidate)) as connection:
        record = youtube_metadata.preserved_capture(connection, 'one')['payload']['record']
        assert record['path'] == 'same/path.mp4'
        assert 'metadata_version' not in record and 'duration_seconds' not in record and 'layout_version' not in record
    absent = tmp_path / 'without-youtube.sqlite'
    make_source(absent)
    assert migrations.prepare_essential_candidate(absent, tmp_path / 'absent', catalog_importers=[capture_importer()]).imported_items == {'youtube.saved-media': 0}


@pytest.mark.parametrize('broken', ['missing', 'cycle', 'root', 'url', 'source_id', 'video', 'audio'])
def test_broken_capture_ancestry_refuses_with_every_new_row_rolled_back(captured_youtube_source, tmp_path, broken):
    with closing(sqlite3.connect(captured_youtube_source)) as connection:
        if broken == 'missing':
            connection.execute("UPDATE youtube_downloads SET retry_of='unknown-parent' WHERE id='one'")
        elif broken == 'cycle':
            connection.execute("UPDATE youtube_downloads SET retry_of='one' WHERE id='queued'")
        else:
            connection.execute('UPDATE youtube_downloads SET ' + broken + "=? WHERE id='failed'", ('foreign-value',))
        connection.commit()
    original = captured_youtube_source.read_bytes()
    with pytest.raises(migrations.PreparationError, match='YouTube retry history'):
        migrations.prepare_essential_candidate(captured_youtube_source, tmp_path / 'attempt', catalog_importers=[capture_importer()])
    with closing(sqlite3.connect(tmp_path / 'attempt/essential.sqlite')) as connection:
        for table in ('kv_items', 'kv_legacy_keys', 'kv_item_captures', 'kv_libraries'):
            assert connection.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0
    assert captured_youtube_source.read_bytes() == original


def test_owner_capture_interruption_can_resume_without_duplicate_history(captured_youtube_source, tmp_path):
    def interrupted(connection):
        import_catalog_captures(connection)
        raise RuntimeError('interrupted after captured facts')
    attempt = tmp_path / 'attempt'
    with pytest.raises(migrations.PreparationError, match='interrupted after captured facts'):
        migrations.prepare_essential_candidate(captured_youtube_source, attempt, catalog_importers=[capture_importer(interrupted)])
    with closing(sqlite3.connect(attempt / 'essential.sqlite')) as connection:
        assert connection.execute('SELECT count(*) FROM kv_item_captures').fetchone()[0] == 0
    prepared = migrations.prepare_essential_candidate(captured_youtube_source, attempt, catalog_importers=[capture_importer()])
    with closing(sqlite3.connect(prepared.candidate)) as connection:
        assert connection.execute('SELECT count(*) FROM kv_item_captures').fetchone()[0] == 2
        assert youtube_metadata.preserved_capture(connection, 'one')['payload']['record']['metadata_version'] == 1


def test_changed_capture_facts_cannot_replace_existing_history(youtube_source):
    with closing(sqlite3.connect(youtube_source)) as connection:
        migrations.item_catalog.ensure_catalog_schema(connection)
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            import_catalog_captures(connection)
        original = youtube_metadata.preserved_capture(connection, 'one')
        connection.execute("UPDATE youtube_downloads SET title='changed after capture' WHERE id='one'")
        connection.commit()
        with pytest.raises(migrations.item_catalog.CatalogConflict):
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                import_catalog_captures(connection)
        assert youtube_metadata.preserved_capture(connection, 'one') == original


@pytest.mark.parametrize('payload,version', [
    ({'legacy_job_ids': ['job', 'job'], 'record': {'id': 'job'}}, 1),
    ({'legacy_job_ids': ['other'], 'record': {'id': 'job'}}, 1),
    ({'legacy_job_ids': ['job'], 'record': {'id': 'other'}}, 1),
    ({'legacy_job_ids': ['job'], 'record': {'id': 'job'}}, 2)])
def test_owner_reader_refuses_invalid_relationships_and_future_formats(tmp_path, payload, version):
    with closing(sqlite3.connect(':memory:')) as connection:
        migrations.item_catalog.ensure_catalog_schema(connection)
        identity = migrations.item_catalog.ensure_item(connection, namespace='youtube.download', legacy_key='job', owner='youtube', item_type='saved-media')
        migrations.item_catalog.record_capture(connection, item_id=identity, namespace='youtube.download', capture_key='job', format_version=version, payload=payload)
        with pytest.raises(ValueError, match='preserved YouTube capture|YouTube capture relationships'):
            youtube_metadata.preserved_capture(connection, 'job')


def test_capture_ancestry_depth_is_bounded(captured_youtube_source, tmp_path):
    with closing(sqlite3.connect(captured_youtube_source)) as connection:
        connection.execute("UPDATE youtube_downloads SET retry_of='parent-0' WHERE id='one'")
        row = connection.execute("SELECT url,source_id,root,video,audio FROM youtube_downloads WHERE id='one'").fetchone()
        for number in range(100):
            parent = 'parent-' + str(number)
            previous = 'parent-' + str(number + 1) if number < 99 else ''
            connection.execute('''INSERT INTO youtube_downloads(id,url,source_id,root,video,audio,status,retry_of)
                VALUES(?,?,?,?,?,?,'failed',?)''', (parent, *row, previous))
        connection.commit()
    with pytest.raises(migrations.PreparationError, match='too deep'):
        migrations.prepare_essential_candidate(captured_youtube_source, tmp_path / 'attempt', catalog_importers=[capture_importer()])


def test_capture_helpers_import_without_runtime_profile_modules():
    backend = str(Path(__file__).resolve().parents[1] / 'backend')
    code = """import sqlite3, sys
sys.path.insert(0, sys.argv[1])
class RefuseRuntime:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ('config', 'database', 'files_base.index'):
            raise AssertionError('Unexpected runtime import: ' + fullname)
sys.meta_path.insert(0, RefuseRuntime())
from modules.youtube import SCHEMA
from modules.youtube.user_schema import import_catalog_captures
from modules.youtube.metadata import preserved_capture
from modules.youtube.metadata import preserved_media
from services import item_catalog
connection = sqlite3.connect(':memory:')
connection.executescript(SCHEMA)
connection.execute(\"INSERT INTO youtube_downloads(id,url,source_id,root,video,audio,status) VALUES('saved','old-url','source','old-root','best','best','complete')\")
connection.commit()
item_catalog.ensure_catalog_schema(connection)
connection.execute('BEGIN IMMEDIATE')
assert import_catalog_captures(connection) == 1
connection.commit()
assert preserved_capture(connection, 'saved')['payload']['record']['url'] == 'old-url'
try:
    preserved_media(connection, 'saved', source_id='foreign', source_root='never-resolve', forbidden_roots=())
except ValueError as error:
    assert 'another registered source' in str(error)
else:
    raise AssertionError('Foreign source accepted')
assert 'config' not in sys.modules and 'database' not in sys.modules
connection.close()
"""
    options = dict(capture_output=True, timeout=20)
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run([sys.executable, '-c', code, backend], **options)
    assert result.returncode == 0, result.stderr


def media_catalog(record, owners, path=':memory:'):
    connection = sqlite3.connect(path)
    migrations.item_catalog.ensure_catalog_schema(connection)
    item = migrations.item_catalog.ensure_item(connection, namespace='youtube.download', legacy_key=record['id'],
                                               owner='youtube', item_type='saved-media')
    migrations.item_catalog.record_capture(connection, item_id=item, namespace='youtube.download', capture_key=record['id'],
        format_version=1, payload=dict(legacy_job_ids=owners, record=record))
    return connection


@pytest.fixture
def saved_catalog_media():
    import tempfile
    from types import SimpleNamespace
    from modules.youtube import saved_item

    # Short roots also exercise the existing Windows portable-path budget.
    with tempfile.TemporaryDirectory(prefix='k54-') as temporary:
        directory = Path(temporary)
        root = directory / 'media'
        root.mkdir()
        owners = ['a' * 32, 'b' * 32]
        bundle = 'YouTube/Channel/Saved [' + owners[1][:16] + ']'
        folder = root / bundle
        (folder / '.keivotos').mkdir(parents=True)
        (folder / 'source.webm').write_bytes(b'preserved media bytes')
        (folder / 'cover.png').write_bytes(b'preserved cover bytes')
        (folder / '.keivotos/channel-avatar.png').write_bytes(b'preserved avatar bytes')
        url = 'https://www.youtube.com/watch?v=abcdefghijk'
        facts = youtube_metadata.capture(dict(id='abcdefghijk', channel_id='UC' + 'a' * 22,
            channel='Captured channel', duration=0, upload_date='20261020'), url, 'source.webm')
        facts.update(avatar='.keivotos/channel-avatar.png', avatar_error='')
        media, thumbnail, digest = saved_item.prepare(folder, dict(id=owners[1], url=url, video='best'),
            dict(path='source.webm', thumbnail='cover.png', title='한국어 saved title'), facts)
        record = dict(id=owners[0], url=url, source_id='src-first', root='/unavailable/original-root',
            video='best', audio='best', title='한국어 saved title', path=bundle + '/' + media,
            thumbnail=bundle + '/' + thumbnail, created_at='2026-10-22T00:00:00Z', retry_of=owners[1],
            **facts, layout_version=1, bundle_path=bundle, manifest_sha256=digest)
        record['avatar'] = bundle + '/' + facts['avatar']
        yield SimpleNamespace(root=root, directory=directory, record=record, owners=owners)


def read_catalog_media(case, *, record=None, forbidden=()):
    record = record if record is not None else case.record
    with closing(media_catalog(record, case.owners)) as connection:
        return youtube_metadata.preserved_media(connection, record['id'], source_id='src-first',
                                               source_root=case.root, forbidden_roots=forbidden)


def test_catalog_only_media_reads_exact_bundle_assets_without_jobs(saved_catalog_media):
    case = saved_catalog_media
    database = case.directory / 'catalog.sqlite'
    with closing(media_catalog(case.record, case.owners, database)):
        pass
    before = database.read_bytes()
    contents = {path.relative_to(case.root).as_posix(): path.read_bytes() for path in case.root.rglob('*') if path.is_file()}
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = lambda cursor, row: {column[0]: value for column, value in zip(cursor.description, row)}
        statements = []
        connection.set_trace_callback(statements.append)
        result = youtube_metadata.preserved_media(connection, case.record['id'], source_id='src-first',
            source_root=case.root, forbidden_roots=())
        assert result['record'] == case.record
        assert set(result['assets']) == {'media', 'thumbnail', 'avatar', 'manifest'}
        for asset in result['assets'].values():
            content = contents[asset['path']]
            assert asset['sha256'] == hashlib.sha256(content).hexdigest() and asset['byte_size'] == len(content)
        assert all(statement.lstrip().upper().startswith('SELECT') and 'youtube_downloads' not in statement for statement in statements)
        assert connection.total_changes == 0
        assert youtube_metadata.preserved_media(connection, 'unknown', source_id='src-first',
            source_root=case.root, forbidden_roots=()) is None
    assert database.read_bytes() == before
    assert contents == {path.relative_to(case.root).as_posix(): path.read_bytes() for path in case.root.rglob('*') if path.is_file()}


def test_catalog_media_uses_current_registered_root_after_relocation(saved_catalog_media):
    import shutil
    case = saved_catalog_media
    relocated = case.directory / 'relocated'
    shutil.copytree(case.root, relocated)
    with closing(media_catalog(case.record, case.owners)) as connection:
        before = youtube_metadata.preserved_media(connection, case.record['id'], source_id='src-first',
            source_root=case.root, forbidden_roots=())
        result = youtube_metadata.preserved_media(connection, case.record['id'], source_id='src-first',
            source_root=relocated, forbidden_roots=(case.root,))
    assert result == before and result['record']['root'] == '/unavailable/original-root'


@pytest.mark.parametrize('extension', ['webm', 'm4a'])
def test_catalog_media_keeps_old_flat_outputs_and_retry_owner(saved_catalog_media, extension):
    case = saved_catalog_media
    relative = 'YouTube/' + case.owners[1] + '/original.' + extension
    path = case.root / relative
    path.parent.mkdir()
    path.write_bytes(b'old flat completed bytes')
    record = {name: value for name, value in case.record.items()
              if name not in (*youtube_metadata.COLUMNS, 'bundle_path', 'manifest_sha256', 'layout_version')}
    record.update(path=relative, thumbnail='')
    result = read_catalog_media(case, record=record)
    assert set(result['assets']) == {'media'}
    assert result['assets']['media']['path'] == relative


@pytest.mark.parametrize('role', ['thumbnail', 'avatar'])
@pytest.mark.parametrize('loss', ['missing', 'empty', 'directory'])
def test_catalog_media_optional_art_loss_retains_media(saved_catalog_media, role, loss):
    case = saved_catalog_media
    path = case.root / case.record[role]
    path.unlink()
    if loss == 'empty':
        path.write_bytes(b'')
    elif loss == 'directory':
        path.mkdir()
    result = read_catalog_media(case)
    assert result['assets'][role] is None
    assert result['record'][role] == case.record[role]
    assert result['assets']['media']['byte_size'] > 0


@pytest.mark.parametrize('hazard', ['missing', 'empty', 'directory', 'corrupt', 'foreign-owner', 'changed-facts', 'unknown-version'])
def test_catalog_media_refuses_invalid_manifest_without_rewriting(saved_catalog_media, hazard):
    case = saved_catalog_media
    manifest = case.root / case.record['bundle_path'] / '.keivotos/item.json'
    record = dict(case.record)
    if hazard in ('missing', 'empty', 'directory'):
        manifest.unlink()
        if hazard == 'empty':
            manifest.write_bytes(b'')
        elif hazard == 'directory':
            manifest.mkdir()
    elif hazard == 'corrupt':
        manifest.write_bytes(b'{}')
    else:
        value = json.loads(manifest.read_bytes())
        if hazard == 'foreign-owner':
            value['saved_item_id'] = 'f' * 32
        elif hazard == 'changed-facts':
            value['capture']['channel_name'] = 'Changed source facts'
        else:
            value['version'] = 2
        content = json.dumps(value).encode()
        manifest.write_bytes(content)
        record['manifest_sha256'] = hashlib.sha256(content).hexdigest()
    before = {str(path): path.read_bytes() for path in case.root.rglob('*') if path.is_file()}
    with pytest.raises((ValueError, OSError)):
        read_catalog_media(case, record=record)
    assert before == {str(path): path.read_bytes() for path in case.root.rglob('*') if path.is_file()}


@pytest.mark.parametrize('hazard', ['missing', 'empty', 'directory'])
def test_catalog_media_refuses_unavailable_required_output(saved_catalog_media, hazard):
    case = saved_catalog_media
    media = case.root / case.record['path']
    media.unlink()
    if hazard == 'empty':
        media.write_bytes(b'')
    elif hazard == 'directory':
        media.mkdir()
    with pytest.raises(ValueError):
        read_catalog_media(case)


@pytest.mark.parametrize('role', ['media', 'thumbnail', 'avatar', 'manifest', 'bundle'])
def test_catalog_media_honors_private_and_nested_source_fences(saved_catalog_media, role):
    case = saved_catalog_media
    relative = (case.record['path'] if role == 'media' else case.record['bundle_path'] + '/.keivotos/item.json'
                if role == 'manifest' else case.record['bundle_path'] if role == 'bundle' else case.record[role])
    from files_base.serving import ServeDenied
    with pytest.raises(ServeDenied):
        read_catalog_media(case, forbidden=(case.root / relative,))


@pytest.mark.parametrize('change', ['../outside.webm', '/outside.webm', 'YouTube/channel/../outside.webm',
                                     'YouTube\\channel\\outside.webm', 'youtube/channel/video.webm'])
def test_catalog_media_rejects_changed_relationships(saved_catalog_media, change):
    case = saved_catalog_media
    record = dict(case.record, path=change)
    with pytest.raises(ValueError):
        read_catalog_media(case, record=record)


def test_catalog_media_checks_source_identity_before_touching_files(saved_catalog_media, monkeypatch):
    case = saved_catalog_media
    def refuse_resolution(*args, **kwargs):
        raise AssertionError('Wrong source must not access a filesystem path')
    with closing(media_catalog(case.record, case.owners)) as connection, monkeypatch.context() as patcher:
        patcher.setattr(Path, 'resolve', refuse_resolution)
        with pytest.raises(ValueError, match='another registered source'):
            youtube_metadata.preserved_media(connection, case.record['id'], source_id='foreign',
                source_root=case.root, forbidden_roots=())


@pytest.mark.parametrize('role', ['media', 'manifest'])
def test_catalog_media_refuses_symbolic_links(saved_catalog_media, role):
    case = saved_catalog_media
    relative = case.record['path'] if role == 'media' else case.record['bundle_path'] + '/.keivotos/item.json'
    path = case.root / relative
    outside = case.directory / ('outside-' + role)
    outside.write_bytes(path.read_bytes())
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError as error:
        if os.name == 'nt' and error.winerror == 1314:
            pytest.skip('Native Windows token cannot create symbolic links; exercised on Linux')
        raise
    from files_base.serving import ServeDenied
    with pytest.raises((ValueError, ServeDenied)):
        read_catalog_media(case)


def test_catalog_media_refuses_changes_during_streamed_observation(saved_catalog_media, monkeypatch):
    case = saved_catalog_media
    original = hashlib.file_digest
    def changed(stream, algorithm):
        result = original(stream, algorithm)
        if str(stream.name).endswith('video.webm'):
            with Path(stream.name).open('ab') as output:
                output.write(b'changed while validating')
        return result
    monkeypatch.setattr(hashlib, 'file_digest', changed)
    with pytest.raises(ValueError, match='changed during validation'):
        read_catalog_media(case)


def asset_catalog(case, path=':memory:'):
    connection = media_catalog(case.record, case.owners, path)
    migrations.item_catalog.ensure_library(connection, namespace='files.source', legacy_key='src-first',
                                          library_id='src-first', kind='linked', display_name='Saved library')
    return connection


def register_media_assets(connection, case, **changes):
    return register_catalog_assets(connection, case.record['id'], **(dict(source_id='src-first',
        source_root=case.root, forbidden_roots=()) | changes))


def asset_rows(connection):
    return {table: connection.execute('SELECT * FROM ' + table).fetchall()
            for table in ('kv_assets', 'kv_item_assets', 'kv_item_captures')}


def test_owner_asset_registration_is_job_independent_exact_and_nonmutating(saved_catalog_media):
    case = saved_catalog_media
    files = {str(path): path.read_bytes() for path in case.root.rglob('*') if path.is_file()}
    with closing(asset_catalog(case)) as connection:
        original = youtube_metadata.preserved_capture(connection, case.record['id'])
        observed = register_media_assets(connection, case)
        before = asset_rows(connection)
        assert register_media_assets(connection, case) == observed
        assert asset_rows(connection) == before
        assert youtube_metadata.preserved_capture(connection, case.record['id']) == original
        assert len(before['kv_assets']) == 4 and len(before['kv_item_assets']) == 4
        assert observed['library_id'] == 'src-first'
        snapshot = migrations.item_catalog.read_capture(connection, item_id=observed['item_id'],
            namespace='youtube.asset-observation', capture_key=observed['capture_key'])
        assert snapshot['payload'] == dict(legacy_id=case.record['id'], source_id='src-first',
                                           library_id='src-first', assets=observed['assets'])
        assert str(case.root) not in json.dumps(snapshot['payload'])
        assert '/unavailable/original-root' not in json.dumps(snapshot['payload'])
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='youtube_downloads'").fetchone()
        types = dict(connection.execute('SELECT a.role,b.media_type FROM kv_item_assets a JOIN kv_assets b ON a.asset_id=b.id'))
        assert types == dict(media='video/webm', thumbnail='image/png', avatar='image/png', manifest='application/json')
    assert files == {str(path): path.read_bytes() for path in case.root.rglob('*') if path.is_file()}


def test_owner_asset_registration_survives_restart_and_relocated_current_root(saved_catalog_media):
    import shutil
    case = saved_catalog_media
    database = case.directory / 'registered.sqlite'
    with closing(asset_catalog(case, database)) as connection:
        original = register_media_assets(connection, case)
    relocated = case.directory / 'moved'
    shutil.copytree(case.root, relocated)
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = lambda cursor, row: dict(zip([column[0] for column in cursor.description], row))
        result = register_media_assets(connection, case, source_root=relocated, forbidden_roots=(case.root,))
        assert result == original
        assert connection.row_factory is not None
        assert connection.execute('SELECT count(*) AS n FROM kv_item_captures').fetchone()['n'] == 2


@pytest.mark.parametrize('role', ['thumbnail', 'avatar'])
def test_optional_asset_loss_and_restore_preserve_ids_links_and_observation_history(saved_catalog_media, role):
    case = saved_catalog_media
    path = case.root / case.record[role]
    original_bytes = path.read_bytes()
    with closing(asset_catalog(case)) as connection:
        original = register_media_assets(connection, case)
        links = connection.execute('SELECT * FROM kv_item_assets').fetchall()
        path.unlink()
        missing = register_media_assets(connection, case)
        assert missing['assets'][role] is None and missing['capture_key'] != original['capture_key']
        assert connection.execute('SELECT * FROM kv_item_assets').fetchall() == links
        assert connection.execute('SELECT count(*) FROM kv_assets').fetchone()[0] == 4
        assert connection.execute('SELECT count(*) FROM kv_item_captures').fetchone()[0] == 3
        assert register_media_assets(connection, case) == missing
        path.write_bytes(original_bytes)
        assert register_media_assets(connection, case) == original
        assert connection.execute('SELECT count(*) FROM kv_item_captures').fetchone()[0] == 3


def test_previously_missing_optional_art_can_be_registered_without_replacing_earlier_observation(saved_catalog_media):
    case = saved_catalog_media
    path = case.root / case.record['avatar']
    content = path.read_bytes()
    path.unlink()
    with closing(asset_catalog(case)) as connection:
        missing = register_media_assets(connection, case)
        assert len(connection.execute('SELECT * FROM kv_assets').fetchall()) == 3
        path.write_bytes(content)
        present = register_media_assets(connection, case)
        assert present['assets']['avatar'] is not None
        assert present['capture_key'] != missing['capture_key']
        assert connection.execute('SELECT count(*) FROM kv_assets').fetchone()[0] == 4
        preserved = migrations.item_catalog.read_capture(connection, item_id=missing['item_id'],
            namespace='youtube.asset-observation', capture_key=missing['capture_key'])
        assert preserved['payload']['assets']['avatar'] is None


@pytest.mark.parametrize('role', ['media', 'thumbnail', 'avatar'])
def test_changed_registered_asset_bytes_refuse_without_new_rows_or_original_replacement(saved_catalog_media, role):
    case = saved_catalog_media
    with closing(asset_catalog(case)) as connection:
        register_media_assets(connection, case)
        before = asset_rows(connection)
        relative = case.record['path'] if role == 'media' else case.record[role]
        path = case.root / relative
        path.write_bytes(path.read_bytes() + b'new observed bytes')
        changed = path.read_bytes()
        with pytest.raises(migrations.item_catalog.CatalogConflict, match='different bytes'):
            register_media_assets(connection, case)
        assert asset_rows(connection) == before and path.read_bytes() == changed


def test_owner_asset_registration_retains_caller_rollback_and_capture_failure_is_atomic(saved_catalog_media):
    case = saved_catalog_media
    with closing(asset_catalog(case)) as connection:
        before = asset_rows(connection)
        connection.execute('BEGIN IMMEDIATE')
        register_media_assets(connection, case)
        assert connection.in_transaction and connection.execute('SELECT count(*) FROM kv_assets').fetchone()[0] == 4
        connection.rollback()
        assert asset_rows(connection) == before
        connection.executescript("CREATE TRIGGER reject_observation BEFORE INSERT ON kv_item_captures WHEN new.namespace='youtube.asset-observation' BEGIN SELECT RAISE(ABORT,'snapshot failed'); END;")
        with pytest.raises(sqlite3.IntegrityError, match='snapshot failed'):
            register_media_assets(connection, case)
        assert asset_rows(connection) == before


def test_owner_asset_registration_requires_known_library_and_honors_source_fences(saved_catalog_media, monkeypatch):
    case = saved_catalog_media
    with closing(media_catalog(case.record, case.owners)) as connection, monkeypatch.context() as patcher:
        def forbidden_resolution(*args, **kwargs):
            raise AssertionError('Unknown library must refuse before filesystem access')
        patcher.setattr(Path, 'resolve', forbidden_resolution)
        with pytest.raises(ValueError, match='source library'):
            register_media_assets(connection, case)
        assert connection.execute('SELECT count(*) FROM kv_assets').fetchone()[0] == 0
    from files_base.serving import ServeDenied
    with closing(asset_catalog(case)) as connection:
        with pytest.raises(ServeDenied):
            register_media_assets(connection, case, forbidden_roots=(case.root / case.record['bundle_path'],))
        with pytest.raises(ValueError, match='source library'):
            register_media_assets(connection, case, source_id='other-source')
        assert connection.execute('SELECT count(*) FROM kv_assets').fetchone()[0] == 0
        assert register_catalog_assets(connection, 'unknown', source_id='src-first', source_root=case.root, forbidden_roots=()) is None


def test_separate_saved_items_never_merge_assets_because_bytes_and_paths_match(saved_catalog_media):
    case = saved_catalog_media
    with closing(asset_catalog(case)) as connection:
        first = register_media_assets(connection, case)
        other_id = 'c' * 32
        record = dict(case.record, id=other_id, retry_of=case.owners[0])
        identity = migrations.item_catalog.ensure_item(connection, namespace='youtube.download', legacy_key=other_id,
            owner='youtube', item_type='saved-media')
        migrations.item_catalog.record_capture(connection, item_id=identity, namespace='youtube.download',
            capture_key=other_id, format_version=1, payload=dict(legacy_job_ids=[other_id, *case.owners], record=record))
        other = register_catalog_assets(connection, other_id, source_id='src-first', source_root=case.root, forbidden_roots=())
        assert first['item_id'] != other['item_id']
        for role, asset in first['assets'].items():
            assert asset['sha256'] == other['assets'][role]['sha256']
            assert asset['asset_id'] != other['assets'][role]['asset_id']
        assert connection.execute('SELECT count(*) FROM kv_assets').fetchone()[0] == 8


@pytest.mark.parametrize('interrupted', [False, True])
def test_asset_registration_integrates_with_candidate_preparation_and_preserves_legacy(essential_source, saved_catalog_media, tmp_path, interrupted):
    from modules.youtube.user_schema import migrate
    case = saved_catalog_media
    with closing(sqlite3.connect(essential_source)) as connection:
        connection.executescript(YOUTUBE_SCHEMA + YOUTUBE_PERSONAL_SCHEMA)
        migrate(connection)
        connection.execute("UPDATE files_sources SET path=? WHERE source_id='src-first'", (str(case.root),))
        values = dict(case.record, status='complete')
        connection.execute('INSERT INTO youtube_downloads(' + ','.join(values) + ') VALUES(' + ','.join('?' for _ in values) + ')', tuple(values.values()))
        connection.execute("INSERT INTO youtube_downloads(id,url,source_id,root,video,audio,status) VALUES(?,?,?,?,?,?,'failed')",
            (case.owners[1], *[case.record[name] for name in ('url', 'source_id', 'root', 'video', 'audio')]))
        connection.execute("INSERT INTO youtube_playlists(id,request_id,title) VALUES('kept-playlist','kept-request','Original title')")
        connection.execute('INSERT INTO youtube_playlist_items(playlist_id,job_id,position) VALUES(?,?,0)', ('kept-playlist', case.record['id']))
        connection.commit()
        before = migrations._state(connection)
        objects = {(row[0], row[1]) for row in connection.execute('SELECT type,name FROM sqlite_master WHERE sql IS NOT NULL')}
    original = essential_source.read_bytes()
    def import_assets(connection):
        count = import_catalog_captures(connection)
        register_media_assets(connection, case)
        if interrupted:
            raise RuntimeError('interrupted after asset registration')
        return count
    importer = migrations.CatalogImporter('youtube.saved-assets', 1, import_assets)
    if interrupted:
        attempt = tmp_path / 'interrupted-assets'
        with pytest.raises(migrations.PreparationError, match='interrupted after asset registration'):
            migrations.prepare_essential_candidate(essential_source, attempt, catalog_importers=[importer])
        with closing(sqlite3.connect(attempt / 'essential.sqlite')) as connection:
            for table in ('kv_libraries', 'kv_items', 'kv_assets', 'kv_item_assets', 'kv_item_captures'):
                assert connection.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0
    else:
        registered = []
        for number in range(2):
            prepared = migrations.prepare_essential_candidate(essential_source, tmp_path / ('assets-' + str(number)), catalog_importers=[importer])
            with closing(sqlite3.connect(prepared.candidate)) as connection:
                assert migrations._state(connection, schema_objects=objects, table_names=set(before['table_rows'])) == before
                registered.append(register_media_assets(connection, case))
                assert connection.execute('SELECT job_id FROM youtube_playlist_items').fetchone()[0] == case.record['id']
            assert migrations.validate_essential_candidate(essential_source, prepared.attempt_directory, catalog_importers=[importer]) == prepared
        assert registered[0] == registered[1]
    assert essential_source.read_bytes() == original


def test_old_flat_audio_assets_register_without_inventing_bundle_or_capture_fields(saved_catalog_media):
    case = saved_catalog_media
    folder = 'YouTube/' + case.owners[1]
    (case.root / folder).mkdir()
    (case.root / folder / 'audio.m4a').write_bytes(b'old flat audio')
    (case.root / folder / 'cover.jpg').write_bytes(b'old cover')
    record = {name: value for name, value in case.record.items()
              if name not in (*youtube_metadata.COLUMNS, 'bundle_path', 'manifest_sha256', 'layout_version')}
    record.update(path=folder + '/audio.m4a', thumbnail=folder + '/cover.jpg')
    with closing(media_catalog(record, case.owners)) as connection:
        migrations.item_catalog.ensure_library(connection, namespace='files.source', legacy_key='src-first',
                                              kind='linked', display_name='Old source')
        result = register_media_assets(connection, case)
        assert set(result['assets']) == {'media', 'thumbnail'}
        assert set(dict(connection.execute('SELECT id,media_type FROM kv_assets')).values()) == {'audio/mp4', 'image/jpeg'}
        assert youtube_metadata.preserved_capture(connection, record['id'])['payload']['record'] == record


def playlist_catalog(case, path=':memory:'):
    from modules.youtube import playlists
    connection = asset_catalog(case, path)
    connection.executescript(FILES_SOURCES_SCHEMA + YOUTUBE_PERSONAL_SCHEMA)
    connection.execute("INSERT INTO files_sources(source_id,path,display_name,role) VALUES(?,?,?,'youtube')",
                       ('src-first', str(case.root), 'Current library'))
    connection.commit()
    connection.row_factory = sqlite3.Row
    owner = playlists.create(connection, '한국어 playlist', 'catalog-playlist-request')
    return connection, owner


def save_catalog_membership(connection, job_id, selected, expected):
    from modules.youtube import playlists
    with connection:
        return playlists.set_membership(connection, job_id, selected, expected, use_catalog=True)


def test_catalog_playlist_real_membership_projection_restart_and_readonly_preservation(saved_catalog_media):
    from modules.youtube import playlists
    case = saved_catalog_media
    database = case.directory / 'playlists.sqlite'
    connection, owner = playlist_catalog(case, database)
    with closing(connection):
        catalog_before = asset_rows(connection)
        first = save_catalog_membership(connection, case.record['id'], [owner['id']], 0)
        assert first['version'] == 1
        assert save_catalog_membership(connection, case.record['id'], [owner['id']], 0) == first
        connection.execute('INSERT INTO youtube_playlist_items(playlist_id,job_id,position) VALUES(?,?,1)',
                           (owner['id'], 'missing-captured-item'))
        connection.commit()
        assert asset_rows(connection) == catalog_before
    before = database.read_bytes()
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        queries = []
        connection.set_trace_callback(queries.append)
        result = playlists.items(connection, owner['id'], 0, 60, use_catalog=True)
        assert result['total'] == 2
        assert [row['job_id'] for row in result['items']] == [case.record['id'], 'missing-captured-item']
        saved, missing = result['items']
        assert saved['available'] and saved['position'] == 0
        assert saved['job']['id'] == case.record['id'] and saved['job']['title'] == case.record['title']
        assert saved['job']['status'] == 'complete' and saved['job']['progress'] == 100
        assert saved['job']['error'] == '' and saved['job']['duration_seconds'] == 0
        assert 'root' not in saved['job'] and 'item_id' not in saved['job']
        assert not missing['available'] and missing['job'] is None and missing['unavailable_reason']
        assert playlists.items(connection, owner['id'], 1, 1, use_catalog=True)['items'] == [missing]
        assert playlists.items(connection, owner['id'], 100, 60, use_catalog=True)['items'] == []
        assert all(query.lstrip().upper().startswith('SELECT') and 'youtube_downloads' not in query for query in queries)
    assert database.read_bytes() == before


def test_catalog_playlist_current_registered_root_relocation_preserves_aliases(saved_catalog_media):
    import shutil
    from modules.youtube import playlists
    case = saved_catalog_media
    connection, owner = playlist_catalog(case)
    with closing(connection):
        save_catalog_membership(connection, case.record['id'], [owner['id']], 0)
        relocated = case.directory / 'relocated'
        shutil.copytree(case.root, relocated)
        connection.execute("UPDATE files_sources SET path=? WHERE source_id='src-first'", (str(relocated),))
        connection.commit()
        result = playlists.items(connection, owner['id'], 0, 60, use_catalog=True)
        assert result['items'][0]['available'] and result['items'][0]['job']['path'] == case.record['path']
        assert youtube_metadata.preserved_capture(connection, case.record['id'])['payload']['record']['root'] == '/unavailable/original-root'
        assert playlists.membership(connection, case.record['id']) == dict(version=1, playlist_ids=[owner['id']])


@pytest.mark.parametrize('hazard', ['missing-media', 'hidden-source', 'wrong-role', 'missing-source', 'missing-library',
                                   'nested-media', 'nested-manifest', 'private-root', 'corrupt-manifest', 'empty-media', 'directory'])
def test_catalog_playlist_unavailable_entries_keep_membership_and_version(saved_catalog_media, monkeypatch, hazard):
    from fastapi import HTTPException
    from modules.youtube import playlists
    case = saved_catalog_media
    connection, owner = playlist_catalog(case)
    with closing(connection):
        save_catalog_membership(connection, case.record['id'], [owner['id']], 0)
        media = case.root / case.record['path']
        if hazard in ('missing-media', 'empty-media', 'directory'):
            media.unlink()
            if hazard == 'empty-media': media.write_bytes(b'')
            elif hazard == 'directory': media.mkdir()
        elif hazard == 'hidden-source':
            connection.execute("UPDATE files_sources SET visible=0 WHERE source_id='src-first'")
        elif hazard == 'wrong-role':
            connection.execute("UPDATE files_sources SET role='language' WHERE source_id='src-first'")
        elif hazard == 'missing-source':
            connection.execute("DELETE FROM files_sources WHERE source_id='src-first'")
        elif hazard == 'missing-library':
            connection.execute("DELETE FROM kv_library_sources WHERE legacy_key='src-first'")
        elif hazard in ('nested-media', 'nested-manifest'):
            relative = case.record['path'] if hazard == 'nested-media' else case.record['bundle_path'] + '/.keivotos'
            connection.execute("INSERT INTO files_sources(source_id,path,display_name,role,visible) VALUES('nested',?,'Hidden nested','files',0)",
                               (str(case.root / relative),))
        elif hazard == 'private-root':
            monkeypatch.setattr(playlists.config, 'SUITE_HOME', case.root)
        elif hazard == 'corrupt-manifest':
            (case.root / case.record['bundle_path'] / '.keivotos/item.json').write_bytes(b'{}')
        connection.commit()
        item = playlists.items(connection, owner['id'], 0, 60, use_catalog=True)['items'][0]
        assert not item['available'] and item['unavailable_reason']
        assert playlists.membership(connection, case.record['id']) == dict(version=1, playlist_ids=[owner['id']])
        with pytest.raises(HTTPException) as error:
            save_catalog_membership(connection, case.record['id'], [], 1)
        assert error.value.status_code == 409
        assert connection.execute('SELECT count(*) FROM youtube_playlist_items').fetchone()[0] == 1


@pytest.mark.parametrize('role', ['thumbnail', 'avatar'])
def test_catalog_playlist_missing_optional_art_does_not_block_playback_or_edits(saved_catalog_media, role):
    from modules.youtube import playlists
    case = saved_catalog_media
    connection, owner = playlist_catalog(case)
    with closing(connection):
        (case.root / case.record[role]).unlink()
        save_catalog_membership(connection, case.record['id'], [owner['id']], 0)
        assert playlists.items(connection, owner['id'], 0, 60, use_catalog=True)['items'][0]['available']
        assert save_catalog_membership(connection, case.record['id'], [], 1)['version'] == 2


def test_catalog_playlist_listing_never_streams_whole_media_or_art(saved_catalog_media, monkeypatch):
    from modules.youtube import playlists
    case = saved_catalog_media
    connection, owner = playlist_catalog(case)
    original = hashlib.file_digest
    hashed = []
    def only_manifest(stream, algorithm):
        assert str(stream.name).endswith('item.json'), 'Playlist listing must not hash complete media/art files'
        hashed.append(str(stream.name))
        return original(stream, algorithm)
    monkeypatch.setattr(hashlib, 'file_digest', only_manifest)
    with closing(connection):
        save_catalog_membership(connection, case.record['id'], [owner['id']], 0)
        assert playlists.items(connection, owner['id'], 0, 60, use_catalog=True)['items'][0]['available']
        assert len(hashed) == 2


def test_fast_media_validation_leaves_unread_hashes_explicit_and_registration_default_unchanged(saved_catalog_media):
    case = saved_catalog_media
    with closing(asset_catalog(case)) as connection:
        fast = youtube_metadata.preserved_media(connection, case.record['id'], source_id='src-first',
            source_root=case.root, forbidden_roots=(), hash_assets=False)
        assert fast['assets']['media']['sha256'] is None and fast['assets']['avatar']['sha256'] is None
        assert fast['assets']['manifest']['sha256'] == case.record['manifest_sha256']
        registered = register_media_assets(connection, case)
        assert all(value['sha256'] for value in registered['assets'].values())
        with pytest.raises(ValueError, match='boolean'):
            youtube_metadata.preserved_media(connection, case.record['id'], source_id='src-first',
                source_root=case.root, forbidden_roots=(), hash_assets='false')


def test_catalog_playlist_mode_does_not_consult_or_replace_contradictory_jobs(saved_catalog_media):
    from modules.youtube import playlists
    case = saved_catalog_media
    connection, owner = playlist_catalog(case)
    with closing(connection):
        connection.executescript(YOUTUBE_SCHEMA)
        connection.execute("INSERT INTO youtube_downloads(id,url,source_id,root,video,audio,status,title,path) VALUES(?,?,?,?,?,?,'queued','Different job title','missing.mp4')",
            (case.record['id'], case.record['url'], 'src-first', str(case.root), 'best', 'best'))
        connection.commit()
        save_catalog_membership(connection, case.record['id'], [owner['id']], 0)
        normal = playlists.items(connection, owner['id'], 0, 60)
        catalog = playlists.items(connection, owner['id'], 0, 60, use_catalog=True)
        assert not normal['items'][0]['available'] and normal['items'][0]['job']['title'] == 'Different job title'
        assert catalog['items'][0]['available'] and catalog['items'][0]['job']['title'] == case.record['title']
        assert connection.execute('SELECT status,title FROM youtube_downloads').fetchone()[:] == ('queued', 'Different job title')


def test_catalog_playlist_mutations_keep_existing_revision_recovery_archive_and_rollback(saved_catalog_media):
    from fastapi import HTTPException
    from modules.youtube import playlists
    case = saved_catalog_media
    connection, owner = playlist_catalog(case)
    with closing(connection):
        second = playlists.create(connection, 'Second', 'second-request')
        initial = save_catalog_membership(connection, case.record['id'], [owner['id']], 0)
        assert save_catalog_membership(connection, case.record['id'], [owner['id']], 0) == initial
        with pytest.raises(HTTPException):
            save_catalog_membership(connection, case.record['id'], [second['id']], 0)
        with pytest.raises(HTTPException):
            save_catalog_membership(connection, case.record['id'], ['unknown-playlist'], 1)
        connection.executescript("CREATE TRIGGER fail_catalog_membership BEFORE INSERT ON youtube_playlist_items WHEN new.playlist_id='" + second['id'] + "' BEGIN SELECT RAISE(ABORT,'retained failure'); END;")
        with pytest.raises(sqlite3.IntegrityError):
            save_catalog_membership(connection, case.record['id'], [second['id']], 1)
        assert playlists.membership(connection, case.record['id']) == initial
        renamed = playlists.change(connection, owner['id'], 1, 'rename-request', title='Renamed 한국어')
        assert renamed['revision'] == 2
        with connection:
            assert playlists.change(connection, owner['id'], 1, 'rename-request', title='Renamed 한국어') == renamed
        archived = playlists.change(connection, owner['id'], 2, 'archive-request', count=1)
        assert archived['deleted_at'] and playlists.membership(connection, case.record['id'])['playlist_ids'] == []
        assert connection.execute('SELECT job_id FROM youtube_playlist_items').fetchone()[0] == case.record['id']
        with pytest.raises(HTTPException) as error:
            playlists.items(connection, owner['id'], 0, 60, use_catalog=True)
        assert error.value.status_code == 404


def test_catalog_playlist_concurrent_membership_accepts_one_version(saved_catalog_media):
    from concurrent.futures import ThreadPoolExecutor
    from fastapi import HTTPException
    from modules.youtube import playlists
    case = saved_catalog_media
    database = case.directory / 'concurrent-playlists.sqlite'
    connection, first = playlist_catalog(case, database)
    with closing(connection):
        second = playlists.create(connection, 'Another', 'another-request')
    def save(identity):
        with closing(sqlite3.connect(database, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            try:
                return save_catalog_membership(connection, case.record['id'], [identity], 0)
            except HTTPException as error:
                return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, [first['id'], second['id']]))
    assert sum(isinstance(value, dict) for value in results) == 1 and results.count(409) == 1
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute('SELECT count(*) FROM youtube_playlist_items').fetchone()[0] == 1


def test_catalog_playlist_audio_and_corrupt_capture_remain_unavailable(saved_catalog_media):
    from fastapi import HTTPException
    from modules.youtube import playlists
    case = saved_catalog_media
    connection, owner = playlist_catalog(case)
    with closing(connection):
        identity = migrations.item_catalog.ensure_item(connection, namespace='youtube.download', legacy_key='audio',
                                                      owner='youtube', item_type='saved-media')
        migrations.item_catalog.record_capture(connection, item_id=identity, namespace='youtube.download', capture_key='audio',
            format_version=1, payload=dict(legacy_job_ids=['audio'], record=dict(case.record, id='audio', video='audio')))
        with pytest.raises(HTTPException) as error:
            save_catalog_membership(connection, 'audio', [owner['id']], 0)
        assert error.value.status_code == 409
        save_catalog_membership(connection, case.record['id'], [owner['id']], 0)
        connection.execute("UPDATE kv_item_captures SET payload_sha256=? WHERE capture_key=?", ('00' * 32, case.record['id']))
        connection.commit()
        item = playlists.items(connection, owner['id'], 0, 60, use_catalog=True)['items'][0]
        assert not item['available'] and item['job'] is None
        assert connection.execute('SELECT count(*) FROM youtube_playlist_items').fetchone()[0] == 1


def file_catalog(case, path=':memory:', *, factory=sqlite3.Row):
    from files_base import index
    connection = asset_catalog(case, path)
    connection.executescript(FILES_SOURCES_SCHEMA)
    connection.execute("INSERT INTO files_sources(source_id,path,display_name,role) VALUES(?,?,?,'files')",
                       ('src-first', str(case.root), 'Passive library'))
    connection.commit()
    connection.row_factory = factory
    with index.open_index(case.directory / 'files.sqlite') as indexed:
        index.scan_source(indexed, 'src-first', case.root)
    return connection


def saved_file_context(connection, case, relative, source_id='src-first'):
    from files_base import metadata
    return metadata.context(connection, case.directory / 'files.sqlite', case.directory / 'home', source_id, relative)


def saved_file_registry(case, *, installed=True, broken=False):
    from dataclasses import replace
    from module_registry import ModuleRegistry, build_registry
    registry = build_registry(case.directory / 'home', 'test')
    if not installed:
        return ModuleRegistry((registry.base,))
    def provider(context):
        if broken:
            raise ValueError('Unavailable fixture provider')
        return youtube_metadata.file_metadata(context, use_catalog=True)
    return ModuleRegistry((registry.base, replace(registry.require('youtube'), file_metadata_provider=provider)))


@pytest.mark.parametrize('factory', [sqlite3.Row, lambda cursor, row: dict(zip([column[0] for column in cursor.description], row))],
                         ids=['row', 'dict'])
def test_catalog_file_information_readonly_exact_fields_and_no_jobs(saved_catalog_media, factory):
    from files_base import metadata
    case = saved_catalog_media
    database = case.directory / 'files-personal.sqlite'
    with closing(file_catalog(case, database, factory=factory)):
        pass
    original = database.read_bytes()
    files = {path: path.read_bytes() for path in case.root.rglob('*') if path.is_file()}
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = factory
        statements = []
        connection.set_trace_callback(statements.append)
        ctx = saved_file_context(connection, case, case.record['path'])
        result = metadata.read(ctx, saved_file_registry(case))
        section, = result['sections']
        facts = {field['label']: field for field in section['fields']}
        assert section['owner'] == 'youtube' and section['status'] == 'ready'
        assert facts['Title']['value'] == case.record['title'] and facts['Title']['placement'] == 'origin'
        assert facts['Duration']['value'] == '0:00' and facts['Duration']['placement'] == 'properties'
        assert facts['Source URL']['href'] == case.record['url']
        assert facts['Channel URL']['href'] == youtube_metadata.channel_url(case.record['channel_id'])
        assert facts['Saved file']['value'] == str((case.root / case.record['path']).resolve())
        assert '/unavailable/original-root' not in repr(result)
        assert len(section['related']) == 2 and all(row['available'] for row in section['related'])
        for relative in (case.record['thumbnail'], case.record['bundle_path']):
            other = saved_file_context(connection, case, relative)
            assert metadata.read(other, saved_file_registry(case))['sections'][0]['fields'] == section['fields']
        assert connection.row_factory is factory and connection.total_changes == 0
        assert all(query.lstrip().upper().startswith('SELECT') and 'youtube_downloads' not in query for query in statements)
    assert database.read_bytes() == original
    assert files == {path: path.read_bytes() for path in files}


def test_catalog_file_information_matches_legacy_and_ignores_conflicting_jobs(saved_catalog_media):
    from modules.youtube.user_schema import migrate
    case = saved_catalog_media
    with closing(file_catalog(case)) as connection:
        connection.executescript(YOUTUBE_SCHEMA + YOUTUBE_PERSONAL_SCHEMA)
        migrate(connection)
        values = dict(case.record, status='complete')
        connection.execute('INSERT INTO youtube_downloads(' + ','.join(values) + ') VALUES(' + ','.join('?' for _ in values) + ')',
                           tuple(values.values()))
        connection.execute("INSERT INTO youtube_downloads(id,url,source_id,root,video,audio,status) VALUES(?,?,?,?,?,?,'failed')",
                           (case.owners[1], *[case.record[name] for name in ('url', 'source_id', 'root', 'video', 'audio')]))
        connection.commit()
        ctx = saved_file_context(connection, case, case.record['path'])
        old = youtube_metadata.file_metadata(ctx)
        assert youtube_metadata.file_metadata(ctx, use_catalog=True) == old
        connection.execute("UPDATE youtube_downloads SET status='queued',title='Conflicting job'")
        connection.commit()
        assert youtube_metadata.file_metadata(ctx) is None
        assert youtube_metadata.file_metadata(ctx, use_catalog=True) == old
        assert connection.execute('SELECT DISTINCT title FROM youtube_downloads').fetchone()[0] == 'Conflicting job'


def test_catalog_file_information_uses_relocated_registered_root(saved_catalog_media):
    import shutil
    from files_base import index, metadata
    case = saved_catalog_media
    with closing(file_catalog(case)) as connection:
        relocated = case.directory / 'relocated'
        shutil.copytree(case.root, relocated)
        connection.execute("UPDATE files_sources SET path=? WHERE source_id='src-first'", (str(relocated),))
        connection.commit()
        with index.open_index(case.directory / 'files.sqlite') as indexed:
            index.scan_source(indexed, 'src-first', relocated)
        ctx = saved_file_context(connection, case, case.record['path'])
        result = metadata.read(ctx, saved_file_registry(case))['sections'][0]
        fields = {field['label']: field['value'] for field in result['fields']}
        assert fields['Saved file'] == str((relocated / case.record['path']).resolve())
        assert youtube_metadata.preserved_capture(connection, case.record['id'])['payload']['record'] == case.record


@pytest.mark.parametrize('relative', ['', 'unrelated.webm', 'VIDEO.WEBM', '.keivotos/channel-avatar.png'])
def test_catalog_file_lookup_never_infers_unrecorded_relationships(saved_catalog_media, relative):
    case = saved_catalog_media
    with closing(file_catalog(case)) as connection:
        assert youtube_metadata._preserved_file_capture(connection, 'src-first', relative) is None
        assert youtube_metadata._preserved_file_capture(connection, 'other-source', case.record['path']) is None
        if relative in ('', 'unrelated.webm'):
            if relative:
                from files_base import index
                (case.root / relative).write_bytes(b'unrelated original')
                with index.open_index(case.directory / 'files.sqlite') as indexed:
                    index.scan_source(indexed, 'src-first', case.root)
            ctx = saved_file_context(connection, case, relative)
            assert youtube_metadata.file_metadata(ctx, use_catalog=True) is None


@pytest.mark.parametrize('hazard', ['hash', 'format', 'alias', 'ambiguous', 'manifest', 'library'])
def test_catalog_file_information_refuses_ambiguous_or_corrupt_matches(saved_catalog_media, hazard):
    from files_base import metadata
    case = saved_catalog_media
    with closing(file_catalog(case)) as connection:
        if hazard == 'hash':
            connection.execute("UPDATE kv_item_captures SET payload_sha256=?", ('00' * 32,))
        elif hazard == 'format':
            connection.execute('UPDATE kv_item_captures SET format_version=99')
        elif hazard == 'alias':
            other = migrations.item_catalog.ensure_item(connection, namespace='youtube.download', legacy_key='other',
                                                       owner='youtube', item_type='saved-media')
            connection.execute('UPDATE kv_legacy_keys SET item_id=? WHERE legacy_key=?', (other, case.record['id']))
        elif hazard == 'ambiguous':
            identity = migrations.item_catalog.ensure_item(connection, namespace='youtube.download', legacy_key='other',
                                                          owner='youtube', item_type='saved-media')
            migrations.item_catalog.record_capture(connection, item_id=identity, namespace='youtube.download',
                capture_key='other', format_version=1,
                payload=dict(legacy_job_ids=['other', *case.owners], record=dict(case.record, id='other')))
        elif hazard == 'manifest':
            (case.root / case.record['bundle_path'] / '.keivotos/item.json').write_bytes(b'{}')
        else:
            connection.execute("DELETE FROM kv_library_sources WHERE legacy_key='src-first'")
        connection.commit()
        ctx = saved_file_context(connection, case, case.record['path'])
        section, = metadata.read(ctx, saved_file_registry(case))['sections']
        assert section['status'] == 'unavailable' and section['fields'] == [] and section['related'] == []
        assert connection.execute('SELECT count(*) FROM kv_items').fetchone()[0] >= 1


def test_catalog_file_lookup_unrelated_invalid_json_and_foreign_paths_do_not_claim_subject(saved_catalog_media):
    case = saved_catalog_media
    with closing(file_catalog(case)) as connection:
        identity = youtube_metadata.preserved_capture(connection, case.record['id'])['item_id']
        connection.execute('INSERT INTO kv_item_captures(item_id,namespace,capture_key,format_version,payload_json,payload_sha256) VALUES(?,?,?,1,?,?)',
                           (identity, 'youtube.download', 'unreadable', '{invalid', '00' * 32))
        connection.commit()
        assert youtube_metadata._preserved_file_capture(connection, 'src-first', case.record['path'])['item_id'] == identity
        for source, relative in [('other-source', case.record['path']), ('src-first', '../outside'), ('src-first', case.record['path'].upper())]:
            assert youtube_metadata._preserved_file_capture(connection, source, relative) is None


@pytest.mark.parametrize('hazard', ['hidden', 'nested-subject', 'nested-manifest', 'private', 'unindexed', 'symlink'])
def test_catalog_file_information_keeps_fresh_files_source_fences(saved_catalog_media, hazard):
    from files_base import metadata, serving
    case = saved_catalog_media
    with closing(file_catalog(case)) as connection:
        relative = case.record['path']
        if hazard == 'hidden':
            connection.execute("UPDATE files_sources SET visible=0 WHERE source_id='src-first'")
        elif hazard.startswith('nested'):
            child = (case.root / relative).parent if hazard == 'nested-subject' else case.root / case.record['bundle_path'] / '.keivotos'
            connection.execute("INSERT INTO files_sources(source_id,path,display_name,role,visible) VALUES('child',?,'Hidden child','files',0)", (str(child),))
        elif hazard == 'private':
            connection.execute("UPDATE files_sources SET path=? WHERE source_id='src-first'", (str(case.directory / 'home'),))
            (case.directory / 'home').mkdir()
        elif hazard == 'unindexed':
            relative = 'new.webm'
            (case.root / relative).write_bytes(b'unindexed file')
        else:
            target = case.root / relative
            other = case.directory / 'outside.webm'
            other.write_bytes(b'outside original')
            target.unlink()
            try:
                target.symlink_to(other)
            except OSError as error:
                if os.name == 'nt' and getattr(error, 'winerror', None) == 1314:
                    pytest.skip('Windows token lacks symbolic-link privilege')
                raise
        connection.commit()
        if hazard == 'nested-manifest':
            ctx = saved_file_context(connection, case, relative)
            with pytest.raises(serving.ServeDenied):
                metadata.read(ctx, saved_file_registry(case))
        else:
            with pytest.raises(serving.ServeDenied):
                saved_file_context(connection, case, '' if hazard == 'private' else relative)


def test_catalog_file_information_optional_art_and_missing_media_preserve_captures(saved_catalog_media):
    from files_base import metadata
    case = saved_catalog_media
    with closing(file_catalog(case)) as connection:
        original = youtube_metadata.preserved_capture(connection, case.record['id'])
        (case.root / case.record['thumbnail']).unlink()
        ctx = saved_file_context(connection, case, case.record['bundle_path'])
        section, = metadata.read(ctx, saved_file_registry(case))['sections']
        assert section['status'] == 'ready' and not section['related'][1]['available']
        (case.root / case.record['path']).unlink()
        assert metadata.read(ctx, saved_file_registry(case))['sections'][0]['status'] == 'unavailable'
        assert youtube_metadata.preserved_capture(connection, case.record['id']) == original


@pytest.mark.parametrize('audio', [False, True])
def test_catalog_file_information_old_flat_records_keep_unknown_values(saved_catalog_media, audio):
    from types import SimpleNamespace
    case = saved_catalog_media
    relative = 'YouTube/' + case.record['id'] + ('/audio.m4a' if audio else '/video.webm')
    target = case.root / relative
    target.parent.mkdir()
    target.write_bytes(b'old flat original')
    record = {name: value for name, value in case.record.items()
              if name not in (*youtube_metadata.COLUMNS, 'bundle_path', 'manifest_sha256', 'layout_version', 'retry_of')}
    record.update(path=relative, thumbnail='', video='audio' if audio else 'best')
    old = SimpleNamespace(**dict(case.__dict__, record=record, owners=[record['id']]))
    with closing(file_catalog(old)) as connection:
        result = youtube_metadata.file_metadata(saved_file_context(connection, old, relative), use_catalog=True)
        fields = {field['label']: field['value'] for field in result['fields']}
        assert fields['Channel'] is fields['Downloaded'] is fields['Duration'] is fields['File format'] is None
        assert fields['Requested video'] == record['video'] and fields['Title'] == record['title']
        assert result['related'] == [{'label': 'Downloaded file', 'path': relative}]


def test_catalog_file_information_reads_no_media_or_art_bytes(saved_catalog_media, monkeypatch):
    case = saved_catalog_media
    with closing(file_catalog(case)) as connection:
        ctx = saved_file_context(connection, case, case.record['path'])
        original = Path.open
        opened = []
        def only_manifest(path, *args, **kwargs):
            assert path.name == 'item.json', 'Passive file facts must not read whole media/art files'
            opened.append(path)
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'open', only_manifest)
        assert youtube_metadata.file_metadata(ctx, use_catalog=True)['fields']
        assert len(opened) == 1


def test_catalog_file_information_real_route_absence_failure_and_late_visibility(saved_catalog_media, monkeypatch):
    from contextlib import contextmanager
    from fastapi import HTTPException
    from starlette.responses import Response
    from files_base import metadata
    from routers import files
    case = saved_catalog_media
    database = case.directory / 'route-personal.sqlite'
    with closing(file_catalog(case, database)):
        pass
    @contextmanager
    def connect():
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            yield connection
    monkeypatch.setattr(files, 'get_user_db', connect)
    monkeypatch.setattr(files.config, 'FILES_DB_PATH', case.directory / 'files.sqlite')
    monkeypatch.setattr(files.config, 'SUITE_HOME', case.directory / 'home')
    monkeypatch.setattr(files.config, 'MODULE_REGISTRY', saved_file_registry(case))
    response = Response()
    result = files.get_module_metadata(response, 'src-first', case.record['path'])
    assert result['sections'][0]['owner'] == 'youtube' and response.headers['Cache-Control'] == 'no-store'
    monkeypatch.setattr(files.config, 'MODULE_REGISTRY', saved_file_registry(case, installed=False))
    assert files.get_module_metadata(Response(), 'src-first', case.record['path'])['sections'] == []
    monkeypatch.setattr(files.config, 'MODULE_REGISTRY', saved_file_registry(case, broken=True))
    assert files.get_module_metadata(Response(), 'src-first', case.record['path'])['sections'][0]['status'] == 'unavailable'
    from dataclasses import replace
    registry = saved_file_registry(case)
    def hide(context):
        result = youtube_metadata.file_metadata(context, use_catalog=True)
        context.user_connection.execute("UPDATE files_sources SET visible=0 WHERE source_id='src-first'")
        return result
    monkeypatch.setattr(files.config, 'MODULE_REGISTRY', registry.replacing(replace(registry.require('youtube'), file_metadata_provider=hide)))
    with pytest.raises(HTTPException) as error:
        files.get_module_metadata(Response(), 'src-first', case.record['path'])
    assert error.value.status_code == 404


def test_catalog_file_information_fresh_process_imports_no_jobs_workers_or_network(saved_catalog_media):
    case = saved_catalog_media
    database = case.directory / 'offline.sqlite'
    with closing(file_catalog(case, database)):
        pass
    script = '''
import builtins, socket, sqlite3, sys
from pathlib import Path
sys.path.insert(0, 'backend')
original = builtins.__import__
def guarded(name, *args, **kwargs):
    assert not name.startswith(('modules.youtube.jobs', 'modules.youtube.worker')), name
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(AssertionError('network'))
from files_base import metadata
from modules.youtube.metadata import file_metadata
with sqlite3.connect(Path(sys.argv[1]).as_uri() + '?mode=ro', uri=True) as connection:
    connection.row_factory = sqlite3.Row
    ctx = metadata.context(connection, Path(sys.argv[2]), Path(sys.argv[3]), 'src-first', sys.argv[4])
    assert file_metadata(ctx, use_catalog=True)['fields']
assert not any(name.startswith(('modules.youtube.jobs', 'modules.youtube.worker')) for name in sys.modules)
'''
    subprocess.run([sys.executable, '-c', script, str(database), str(case.directory / 'files.sqlite'),
                    str(case.directory / 'home'), case.record['path']], cwd=Path(__file__).resolve().parents[1], check=True, timeout=30)


def preserved_art_bytes(kind='PNG', *, animated=False):
    from io import BytesIO
    from PIL import Image
    stream = BytesIO()
    extra = dict(save_all=True, append_images=[Image.new('RGB', (24, 24), 'red')]) if animated else {}
    Image.new('RGB', (24, 24), '#7290ab').save(stream, kind, **extra)
    return stream.getvalue()


def art_catalog(case, path=':memory:', *, factory=sqlite3.Row):
    connection = asset_catalog(case, path)
    connection.executescript(FILES_SOURCES_SCHEMA)
    connection.execute("INSERT INTO files_sources(source_id,path,display_name,role) VALUES(?,?,?,'youtube')",
                       ('src-first', str(case.root), 'Current author artwork'))
    connection.commit()
    connection.row_factory = factory
    return connection


def read_preserved_art(connection, case, identity=None, *, suite_home=None):
    from modules.youtube import channel_art
    return channel_art.read_preserved(connection, identity or case.record['id'],
                                      suite_home=suite_home or case.directory / 'home')


@pytest.mark.parametrize('factory', [sqlite3.Row, lambda cursor, row: dict(zip([column[0] for column in cursor.description], row))],
                         ids=['row', 'dict'])
def test_preserved_channel_art_catalog_only_readonly_restart_and_exact_bytes(saved_catalog_media, factory):
    case = saved_catalog_media
    data = preserved_art_bytes()
    (case.root / case.record['avatar']).write_bytes(data)
    database = case.directory / 'art.sqlite'
    with closing(art_catalog(case, database, factory=factory)):
        pass
    original = database.read_bytes()
    files = {path: path.read_bytes() for path in case.root.rglob('*') if path.is_file()}
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = factory
        statements = []
        connection.set_trace_callback(statements.append)
        assert read_preserved_art(connection, case) == (data, 'image/png')
        assert connection.row_factory is factory and connection.total_changes == 0
        assert all(query.lstrip().upper().startswith('SELECT') and 'youtube_downloads' not in query for query in statements)
    assert database.read_bytes() == original and files == {path: path.read_bytes() for path in files}


@pytest.mark.parametrize('kind,extension,audio', [('JPEG', 'jpg', True), ('PNG', 'png', False), ('WEBP', 'webp', True)])
def test_preserved_channel_art_old_flat_formats_and_audio(saved_catalog_media, kind, extension, audio):
    from types import SimpleNamespace
    case = saved_catalog_media
    folder = 'YouTube/' + case.record['id']
    relative = folder + '/.keivotos/channel-avatar.' + extension
    (case.root / relative).parent.mkdir(parents=True)
    data = preserved_art_bytes(kind)
    (case.root / relative).write_bytes(data)
    media = folder + ('/audio.m4a' if audio else '/video.webm')
    (case.root / media).write_bytes(b'old flat original')
    record = {name: value for name, value in case.record.items()
              if name not in (*youtube_metadata.COLUMNS, 'bundle_path', 'manifest_sha256', 'layout_version', 'retry_of')}
    record.update(path=media, thumbnail='', avatar=relative, video='audio' if audio else 'best')
    old = SimpleNamespace(**dict(case.__dict__, record=record, owners=[record['id']]))
    with closing(art_catalog(old)) as connection:
        assert read_preserved_art(connection, old) == (data, 'image/jpeg' if kind == 'JPEG' else 'image/' + extension)


def test_preserved_channel_art_current_audio_retry_manifest_and_relocation(saved_catalog_media):
    import shutil
    from types import SimpleNamespace
    from modules.youtube import saved_item
    case = saved_catalog_media
    bundle = 'YouTube/Audio/Saved [' + case.owners[1][:16] + ']'
    folder = case.root / bundle
    (folder / '.keivotos').mkdir(parents=True)
    (folder / 'audio.m4a').write_bytes(b'preserved audio')
    data = preserved_art_bytes()
    (folder / '.keivotos/channel-avatar.png').write_bytes(data)
    facts = youtube_metadata.capture(dict(id='abcdefghijk', channel_id='UC' + 'a' * 22), case.record['url'], 'audio.m4a')
    facts.update(avatar='.keivotos/channel-avatar.png', avatar_error='')
    media, cover, digest = saved_item.prepare(folder, dict(id=case.owners[1], url=case.record['url'], video='audio'),
        dict(path='audio.m4a', title='Audio title'), facts)
    record = dict(case.record, **facts)
    record.update(video='audio', path=bundle + '/' + media, thumbnail='', title='Audio title',
                  avatar=bundle + '/' + facts['avatar'], bundle_path=bundle, manifest_sha256=digest)
    audio = SimpleNamespace(**dict(case.__dict__, record=record))
    with closing(art_catalog(audio)) as connection:
        assert read_preserved_art(connection, audio) == (data, 'image/png')
        relocated = case.directory / 'relocated'
        shutil.copytree(case.root, relocated)
        connection.execute("UPDATE files_sources SET path=? WHERE source_id='src-first'", (str(relocated),))
        connection.commit()
        assert read_preserved_art(connection, audio) == (data, 'image/png')
        assert youtube_metadata.preserved_capture(connection, record['id'])['payload']['record'] == record


@pytest.mark.parametrize('absence', ['missing', 'empty', 'directory', 'unspecified', 'unknown'])
def test_preserved_channel_art_missing_never_falls_back_to_cover_or_network(saved_catalog_media, monkeypatch, absence):
    from types import SimpleNamespace
    from modules.youtube import channel_art
    case = saved_catalog_media
    asset = case.root / case.record['avatar']
    if absence in ('missing', 'empty', 'directory'):
        asset.unlink()
        if absence == 'empty': asset.write_bytes(b'')
        elif absence == 'directory': asset.mkdir()
    if absence == 'unspecified':
        case = SimpleNamespace(**dict(case.__dict__, record=dict(case.record, avatar='')))
    monkeypatch.setattr(channel_art, 'lookup', lambda *_: pytest.fail('Local artwork requested a remote lookup'))
    monkeypatch.setattr(channel_art, 'read_remote', lambda *_: pytest.fail('Local artwork requested remote bytes'))
    monkeypatch.setattr(channel_art, 'image_type', lambda *_: pytest.fail('Missing author artwork substituted another image'))
    with closing(art_catalog(case)) as connection:
        assert read_preserved_art(connection, case, 'unknown' if absence == 'unknown' else None) is None
        assert (case.root / case.record['thumbnail']).read_bytes() == b'preserved cover bytes'


@pytest.mark.parametrize('hazard', ['missing-source', 'hidden', 'wrong-role', 'library', 'private',
                                   'nested-art', 'nested-media', 'nested-manifest', 'manifest', 'capture'])
def test_preserved_channel_art_source_fences_and_saved_facts(saved_catalog_media, hazard):
    from files_base.serving import ServeDenied
    case = saved_catalog_media
    (case.root / case.record['avatar']).write_bytes(preserved_art_bytes())
    with closing(art_catalog(case)) as connection:
        if hazard == 'missing-source':
            connection.execute("DELETE FROM files_sources WHERE source_id='src-first'")
        elif hazard == 'hidden':
            connection.execute("UPDATE files_sources SET visible=0 WHERE source_id='src-first'")
        elif hazard == 'wrong-role':
            connection.execute("UPDATE files_sources SET role='language' WHERE source_id='src-first'")
        elif hazard == 'library':
            connection.execute("DELETE FROM kv_library_sources WHERE legacy_key='src-first'")
        elif hazard.startswith('nested'):
            relative = case.record['path'] if hazard == 'nested-media' else case.record['avatar'] if hazard == 'nested-art' else case.record['bundle_path'] + '/.keivotos'
            connection.execute("INSERT INTO files_sources(source_id,path,display_name,role,visible) VALUES('child',?,'Hidden child','files',0)", (str(case.root / relative),))
        elif hazard == 'manifest':
            (case.root / case.record['bundle_path'] / '.keivotos/item.json').write_bytes(b'{}')
        elif hazard == 'capture':
            connection.execute("UPDATE kv_item_captures SET payload_sha256=?", ('00' * 32,))
        connection.commit()
        with pytest.raises((ValueError, ServeDenied)):
            read_preserved_art(connection, case, suite_home=case.root if hazard == 'private' else None)


@pytest.mark.parametrize('kind', ['missing', 'empty', 'directory'])
def test_preserved_channel_art_requires_published_media(saved_catalog_media, kind):
    case = saved_catalog_media
    (case.root / case.record['avatar']).write_bytes(preserved_art_bytes())
    media = case.root / case.record['path']
    media.unlink()
    if kind == 'empty': media.write_bytes(b'')
    elif kind == 'directory': media.mkdir()
    with closing(art_catalog(case)) as connection, pytest.raises(ValueError):
        read_preserved_art(connection, case)


@pytest.mark.parametrize('hazard', ['malformed', 'oversized', 'animated', 'pixel-limit', 'extension'])
def test_preserved_channel_art_decodes_bounded_actual_image_bytes(saved_catalog_media, monkeypatch, hazard):
    from modules.youtube import channel_art
    case = saved_catalog_media
    data = b'<html>' if hazard == 'malformed' else preserved_art_bytes('JPEG' if hazard == 'extension' else 'PNG', animated=hazard == 'animated')
    (case.root / case.record['avatar']).write_bytes(data)
    if hazard == 'oversized': monkeypatch.setattr(channel_art, 'MAX_BYTES', len(data) - 1)
    elif hazard == 'pixel-limit': monkeypatch.setattr(channel_art, 'MAX_PIXELS', 100)
    with closing(art_catalog(case)) as connection, pytest.raises(ValueError):
        read_preserved_art(connection, case)


@pytest.mark.parametrize('parent', [False, True], ids=['file', 'directory'])
def test_preserved_channel_art_refuses_symlinks(saved_catalog_media, parent):
    import shutil
    from files_base.serving import ServeDenied
    case = saved_catalog_media
    asset = case.root / case.record['avatar']
    asset.write_bytes(preserved_art_bytes())
    if parent:
        target = asset.parent
        other = case.directory / 'preserved-private'
        shutil.copytree(target, other)
        target.rename(case.directory / 'original-private')
    else:
        target = asset
        other = case.directory / 'outside.png'
        other.write_bytes(asset.read_bytes())
        target.unlink()
    try:
        target.symlink_to(other, target_is_directory=parent)
    except OSError as error:
        if os.name == 'nt' and getattr(error, 'winerror', None) == 1314:
            pytest.skip('Windows token lacks symbolic-link privilege')
        raise
    with closing(art_catalog(case)) as connection, pytest.raises((ValueError, ServeDenied)):
        read_preserved_art(connection, case)


@pytest.mark.parametrize('change', ['hidden', 'role', 'library', 'art-replaced', 'media-missing', 'root', 'nested'])
def test_preserved_channel_art_rechecks_identity_source_and_publication_after_decode(saved_catalog_media, monkeypatch, change):
    import shutil
    from files_base.serving import ServeDenied
    from modules.youtube import channel_art
    case = saved_catalog_media
    asset = case.root / case.record['avatar']
    data = preserved_art_bytes()
    asset.write_bytes(data)
    original = channel_art.image_type
    with closing(art_catalog(case)) as connection:
        def changed(content):
            extension = original(content)
            if change == 'hidden':
                connection.execute("UPDATE files_sources SET visible=0 WHERE source_id='src-first'")
            elif change == 'role':
                connection.execute("UPDATE files_sources SET role='language' WHERE source_id='src-first'")
            elif change == 'library':
                connection.execute("DELETE FROM kv_library_sources WHERE legacy_key='src-first'")
            elif change == 'art-replaced':
                replacement = case.directory / 'replacement.png'
                replacement.write_bytes(data)
                os.replace(replacement, asset)
            elif change == 'media-missing':
                (case.root / case.record['path']).unlink()
            elif change == 'root':
                relocated = case.directory / 'relocated'
                shutil.copytree(case.root, relocated)
                connection.execute("UPDATE files_sources SET path=? WHERE source_id='src-first'", (str(relocated),))
            else:
                connection.execute("INSERT INTO files_sources(source_id,path,display_name,role,visible) VALUES('child',?,'Late child','files',0)", (str(asset.parent),))
            connection.commit()
            return extension
        monkeypatch.setattr(channel_art, 'image_type', changed)
        with pytest.raises((ValueError, ServeDenied)):
            read_preserved_art(connection, case)


def test_preserved_channel_art_detects_change_during_bounded_read(saved_catalog_media, monkeypatch):
    from contextlib import contextmanager
    case = saved_catalog_media
    asset = case.root / case.record['avatar']
    asset.write_bytes(preserved_art_bytes())
    original = Path.open
    class Changed:
        def __init__(self, stream): self.stream = stream
        def fileno(self): return self.stream.fileno()
        def read(self, size):
            data = self.stream.read(size)
            with original(asset, 'ab') as output:
                output.write(b'changed')
            return data
    @contextmanager
    def opening(path, *args, **kwargs):
        with original(path, *args, **kwargs) as stream:
            yield Changed(stream) if path == asset else stream
    with closing(art_catalog(case)) as connection:
        monkeypatch.setattr(Path, 'open', opening)
        with pytest.raises(ValueError, match='changed during reading'):
            read_preserved_art(connection, case)


def test_preserved_channel_art_never_hashes_media_or_opens_a_cover(saved_catalog_media, monkeypatch):
    case = saved_catalog_media
    data = preserved_art_bytes()
    asset = case.root / case.record['avatar']
    asset.write_bytes(data)
    original_open, original_digest = Path.open, hashlib.file_digest
    opened = []
    def opening(path, *args, **kwargs):
        assert path == asset or path.name == 'item.json', 'Author art must not read the video or substitute its cover'
        opened.append(path)
        return original_open(path, *args, **kwargs)
    def digest(stream, algorithm):
        assert str(stream.name).endswith('item.json'), 'Author art must not hash entire media/art files'
        return original_digest(stream, algorithm)
    with closing(art_catalog(case)) as connection:
        monkeypatch.setattr(Path, 'open', opening)
        monkeypatch.setattr(hashlib, 'file_digest', digest)
        assert read_preserved_art(connection, case) == (data, 'image/png')
        assert opened.count(asset) == 1


def test_preserved_channel_art_ignores_contradictory_jobs(saved_catalog_media):
    case = saved_catalog_media
    data = preserved_art_bytes()
    (case.root / case.record['avatar']).write_bytes(data)
    with closing(art_catalog(case)) as connection:
        connection.executescript(YOUTUBE_SCHEMA)
        connection.execute("INSERT INTO youtube_downloads(id,url,source_id,root,video,audio,status,path) VALUES(?,?,?,?,?,?,'failed','missing.mp4')",
                           (case.record['id'], case.record['url'], 'src-first', str(case.root), 'best', 'best'))
        connection.commit()
        assert read_preserved_art(connection, case) == (data, 'image/png')
        assert connection.execute('SELECT status,path FROM youtube_downloads').fetchone()[:] == ('failed', 'missing.mp4')


def test_preserved_channel_art_fresh_process_reads_offline_without_jobs_or_workers(saved_catalog_media):
    case = saved_catalog_media
    (case.root / case.record['avatar']).write_bytes(preserved_art_bytes())
    database = case.directory / 'offline-art.sqlite'
    with closing(art_catalog(case, database)):
        pass
    script = '''
import builtins, socket, sqlite3, sys
from pathlib import Path
sys.path.insert(0, 'backend')
original = builtins.__import__
def guarded(name, *args, **kwargs):
    assert not name.startswith(('modules.youtube.jobs', 'modules.youtube.worker', 'yt_dlp')), name
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(AssertionError('network'))
from modules.youtube import channel_art
channel_art.lookup = channel_art.read_remote = lambda *a, **k: (_ for _ in ()).throw(AssertionError('remote artwork'))
with sqlite3.connect(Path(sys.argv[1]).as_uri() + '?mode=ro', uri=True) as connection:
    connection.row_factory = sqlite3.Row
    data, mime = channel_art.read_preserved(connection, sys.argv[2], suite_home=Path(sys.argv[3]))
    assert data.startswith(b'\\x89PNG') and mime == 'image/png'
assert not any(name.startswith(('modules.youtube.jobs', 'modules.youtube.worker', 'yt_dlp')) for name in sys.modules)
'''
    subprocess.run([sys.executable, '-c', script, str(database), case.record['id'], str(case.directory / 'home')],
                   cwd=Path(__file__).resolve().parents[1], check=True, timeout=30)


def library_importer():
    from modules.youtube.user_schema import import_catalog_library
    return migrations.CatalogImporter('youtube.saved-library', 1, import_catalog_library)


@pytest.fixture
def saved_library_source(captured_youtube_source):
    with closing(sqlite3.connect(captured_youtube_source)) as connection:
        connection.execute("UPDATE youtube_downloads SET title='한국어 100%_Saved',created_at='2026-10-22T01:00:00Z' WHERE id='one'")
        connection.execute("UPDATE youtube_downloads SET title='Alpha _saved',created_at='2026-10-22T01:00:00Z' WHERE id='two'")
        values = [('a', 'complete', 'Alpha % literal', '', '2026-10-23T00:00:00Z', 'best'),
                  ('audio', 'complete', 'Music audio', '', '2026-10-22T01:00:00Z', 'audio'),
                  ('z', 'complete', 'zeta', '', '2026-10-22T01:00:00Z', 'best'),
                  ('hidden-pending', 'complete', 'Hidden pending', 'queued', '2026-10-24', 'best'),
                  ('hidden-failed', 'complete', 'Hidden failed', 'failed', '2026-10-24', 'best'),
                  ('hidden-missing', 'complete', 'Hidden missing', 'missing-history', '2026-10-24', 'best'),
                  ('hidden-complete', 'complete', 'Hidden complete', 'one', '2026-10-24', 'best'),
                  ('cancelled', 'cancelled', 'Cancelled output', '', '2026-10-25', 'best')]
        connection.executemany('''INSERT INTO youtube_downloads
            (id,url,source_id,root,video,audio,status,title,path,superseded_by,created_at)
            VALUES(?,'https://www.youtube.com/watch?v=abcdefghijk','src-first','/unavailable/old-root',?,'best',?,?,?, ?,?)''',
            [(identity, video, status, title, 'same/audio.m4a' if video == 'audio' else 'same/path.mp4', successor, stamp)
             for identity, status, title, successor, stamp, video in values])
        connection.commit()
    return captured_youtube_source


def saved_library_expected(source, q='', offset=0, limit=60):
    with closing(sqlite3.connect(source)) as connection:
        connection.row_factory = sqlite3.Row
        where = "status='complete' AND superseded_by='' AND instr(lower(title),lower(?))>0"
        total = connection.execute('SELECT count(*) FROM youtube_downloads WHERE ' + where, (q,)).fetchone()[0]
        rows = connection.execute('SELECT * FROM youtube_downloads WHERE ' + where + ' ORDER BY created_at DESC,id LIMIT ? OFFSET ?',
                                  (q, limit, offset)).fetchall()
        return dict(items=[dict(row) for row in rows], total=total)


def job_free_library(candidate, path=':memory:'):
    # Copy only catalog tables from generated fixtures. Never drop a job table,
    # alter a candidate or open a live user's database to simulate history loss.
    connection = sqlite3.connect(path)
    migrations.item_catalog.ensure_catalog_schema(connection)
    with closing(sqlite3.connect(candidate)) as source:
        for table in ('kv_items', 'kv_legacy_keys', 'kv_item_captures'):
            for row in source.execute('SELECT * FROM ' + table):
                connection.execute('INSERT INTO ' + table + ' VALUES(' + ','.join('?' for _ in row) + ')', row)
    connection.commit()
    return connection


@pytest.mark.parametrize('factory', [sqlite3.Row, lambda cursor, row: dict(zip([column[0] for column in cursor.description], row))],
                         ids=['row', 'dict'])
def test_saved_library_import_preserves_all_legacy_records_and_fresh_attempt_ids(saved_library_source, tmp_path, factory):
    source = saved_library_source
    original = source.read_bytes()
    with closing(sqlite3.connect(source)) as connection:
        before = migrations._state(connection)
        objects = {(row[0], row[1]) for row in connection.execute('SELECT type,name FROM sqlite_master WHERE sql IS NOT NULL')}
    imports = []
    for number in range(2):
        result = migrations.prepare_essential_candidate(source, tmp_path / ('library-' + str(number)), catalog_importers=[library_importer()])
        assert result.imported_items == {'youtube.saved-library': 9}
        with closing(sqlite3.connect(result.candidate)) as connection:
            assert migrations._state(connection, schema_objects=objects, table_names=set(before['table_rows'])) == before
            imports.append(connection.execute('SELECT item_id,namespace,capture_key,format_version,payload_json,payload_sha256 FROM kv_item_captures ORDER BY namespace,capture_key').fetchall())
            connection.row_factory = factory
            assert youtube_metadata.preserved_library(connection)['total'] == 5
            assert connection.row_factory is factory
        assert migrations.validate_essential_candidate(source, result.attempt_directory, catalog_importers=[library_importer()]) == result
        assert migrations.prepare_essential_candidate(source, result.attempt_directory, catalog_importers=[library_importer()]) == result
    assert imports[0] == imports[1] and source.read_bytes() == original


@pytest.mark.parametrize('factory', [sqlite3.Row, lambda cursor, row: dict(zip([column[0] for column in cursor.description], row))],
                         ids=['row', 'dict'])
def test_saved_library_no_job_readonly_restart_public_shape_and_exact_facts(saved_library_source, tmp_path, factory):
    prepared = migrations.prepare_essential_candidate(saved_library_source, tmp_path / 'candidate', catalog_importers=[library_importer()])
    database = tmp_path / 'job-free.sqlite'
    with closing(job_free_library(prepared.candidate, database)):
        pass
    original = database.read_bytes()
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = factory
        statements = []
        connection.set_trace_callback(statements.append)
        actual = youtube_metadata.preserved_library(connection)
        expected = saved_library_expected(saved_library_source)
        assert actual['total'] == expected['total'] == 5
        assert [row['id'] for row in actual['items']] == ['a', 'audio', 'one', 'two', 'z']
        for saved, legacy in zip(actual['items'], expected['items']):
            assert set(saved) == set(legacy) - {'root'}
            for field in set(saved) - {'status', 'progress', 'error', 'failure_phase', 'superseded_by'}:
                assert saved[field] == legacy[field]
            assert saved['status'] == 'complete' and saved['progress'] == 100
            assert saved['error'] == saved['failure_phase'] == saved['superseded_by'] == ''
            capture = youtube_metadata.preserved_capture(connection, saved['id'])
            assert not set(capture['payload']['record']) & {'status', 'progress', 'error', 'superseded_by'}
        assert connection.row_factory is factory and connection.total_changes == 0
        assert all(query.lstrip().upper().startswith(('SELECT', 'WITH')) and 'youtube_downloads' not in query for query in statements)
    assert database.read_bytes() == original


@pytest.mark.parametrize('q,offset,limit', [('', 0, 2), ('', 2, 2), ('', 100, 2), ('%', 0, 60), ('_', 0, 60),
    ('SAVED', 0, 1), ('한국어', 0, 60), ('absent', 0, 60), ("%' OR 1=1 --", 0, 60)])
def test_saved_library_literal_search_order_count_and_pages(saved_library_source, tmp_path, q, offset, limit):
    prepared = migrations.prepare_essential_candidate(saved_library_source, tmp_path / 'candidate', catalog_importers=[library_importer()])
    with closing(job_free_library(prepared.candidate)) as connection:
        actual = youtube_metadata.preserved_library(connection, q, offset, limit)
        expected = saved_library_expected(saved_library_source, q, offset, limit)
        assert actual['total'] == expected['total']
        assert [row['id'] for row in actual['items']] == [row['id'] for row in expected['items']]


def test_saved_library_hidden_completed_items_do_not_reappear_without_successor_jobs(saved_library_source, tmp_path):
    prepared = migrations.prepare_essential_candidate(saved_library_source, tmp_path / 'candidate', catalog_importers=[library_importer()])
    with closing(job_free_library(prepared.candidate)) as connection:
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='youtube_downloads'").fetchone()
        assert youtube_metadata.preserved_library(connection, 'Hidden') == dict(items=[], total=0)
        for identity, successor in [('hidden-pending', 'queued'), ('hidden-failed', 'failed'),
                                    ('hidden-missing', 'missing-history'), ('hidden-complete', 'one')]:
            capture = youtube_metadata.preserved_capture(connection, identity)
            membership = migrations.item_catalog.read_capture(connection, item_id=capture['item_id'], namespace='youtube.library', capture_key=identity)
            assert membership['payload'] == dict(legacy_id=identity, listed=False, superseded_by=successor)
            assert capture['payload']['record']['title'].startswith('Hidden')
        for identity in ('failed', 'queued', 'cancelled'):
            assert youtube_metadata.preserved_capture(connection, identity) is None


def test_saved_library_listing_never_reads_files_and_only_decodes_the_page(saved_library_source, tmp_path, monkeypatch):
    prepared = migrations.prepare_essential_candidate(saved_library_source, tmp_path / 'candidate', catalog_importers=[library_importer()])
    original = youtube_metadata.preserved_capture
    decoded = []
    def capture(connection, identity):
        decoded.append(identity)
        return original(connection, identity)
    with closing(job_free_library(prepared.candidate)) as connection:
        monkeypatch.setattr(Path, 'open', lambda *_args, **_kwargs: pytest.fail('Saved-library facts must not open files'))
        monkeypatch.setattr(youtube_metadata, 'preserved_media', lambda *_args, **_kwargs: pytest.fail('Facts listing must not inspect media'))
        monkeypatch.setattr(youtube_metadata, 'preserved_capture', capture)
        assert len(youtube_metadata.preserved_library(connection, '', 1, 2)['items']) == 2
        assert decoded == ['audio', 'one']


@pytest.mark.parametrize('hazard', ['facts-hash', 'member-hash', 'facts-format', 'member-format', 'alias',
    'member-id', 'member-nonboolean', 'member-json', 'facts-json', 'missing-member'])
def test_saved_library_refuses_corrupt_or_incomplete_registration(saved_library_source, tmp_path, hazard):
    prepared = migrations.prepare_essential_candidate(saved_library_source, tmp_path / 'candidate', catalog_importers=[library_importer()])
    with closing(job_free_library(prepared.candidate)) as connection:
        namespace = 'youtube.download' if hazard.startswith('facts') else 'youtube.library'
        if hazard.endswith('hash'):
            connection.execute('UPDATE kv_item_captures SET payload_sha256=? WHERE namespace=? AND capture_key=\'a\'', ('00' * 32, namespace))
        elif hazard.endswith('format'):
            connection.execute('UPDATE kv_item_captures SET format_version=99 WHERE namespace=? AND capture_key=\'a\'', (namespace,))
        elif hazard.endswith('json'):
            connection.execute('UPDATE kv_item_captures SET payload_json=\'{broken\' WHERE namespace=? AND capture_key=\'a\'', (namespace,))
        elif hazard == 'alias':
            connection.execute("UPDATE kv_legacy_keys SET item_id=(SELECT item_id FROM kv_legacy_keys WHERE legacy_key='two') WHERE legacy_key='a'")
        elif hazard == 'missing-member':
            connection.execute("DELETE FROM kv_item_captures WHERE namespace='youtube.library' AND capture_key='a'")
        else:
            payload = dict(legacy_id='other' if hazard == 'member-id' else 'a', listed=1 if hazard == 'member-nonboolean' else True, superseded_by='')
            text, digest = migrations.item_catalog._capture_payload(payload)
            connection.execute("UPDATE kv_item_captures SET payload_json=?,payload_sha256=? WHERE namespace='youtube.library' AND capture_key='a'", (text, digest))
        connection.commit()
        with pytest.raises(ValueError):
            youtube_metadata.preserved_library(connection)


def test_saved_library_requires_visibility_bridge_but_empty_catalog_is_valid(captured_youtube_source, tmp_path):
    with closing(sqlite3.connect(':memory:')) as empty:
        migrations.item_catalog.ensure_catalog_schema(empty)
        assert youtube_metadata.preserved_library(empty) == dict(items=[], total=0)
    prepared = migrations.prepare_essential_candidate(captured_youtube_source, tmp_path / 'candidate', catalog_importers=[capture_importer()])
    with closing(job_free_library(prepared.candidate)) as connection, pytest.raises(ValueError, match='membership before reading'):
        youtube_metadata.preserved_library(connection)


def test_saved_library_import_missing_old_column_absent_owner_and_transaction(youtube_source, essential_source, tmp_path):
    from modules.youtube.user_schema import import_catalog_library
    with closing(sqlite3.connect(youtube_source)) as connection:
        connection.execute('ALTER TABLE youtube_downloads DROP COLUMN superseded_by')
        connection.commit()
    original = youtube_source.read_bytes()
    prepared = migrations.prepare_essential_candidate(youtube_source, tmp_path / 'old', catalog_importers=[library_importer()])
    with closing(job_free_library(prepared.candidate)) as connection:
        assert [row['id'] for row in youtube_metadata.preserved_library(connection)['items']] == ['one', 'two']
        assert youtube_metadata.preserved_library(connection)['items'][0]['metadata_version'] == 0
    assert youtube_source.read_bytes() == original
    absent = tmp_path / 'absent.sqlite'
    make_source(absent)
    missing = migrations.prepare_essential_candidate(absent, tmp_path / 'missing', catalog_importers=[library_importer()])
    assert missing.imported_items == {'youtube.saved-library': 0}
    with closing(sqlite3.connect(missing.candidate)) as connection, pytest.raises(ValueError, match='caller transaction'):
        import_catalog_library(connection)


@pytest.mark.parametrize('failure', ['late-exception', 'visibility-conflict', 'invalid-successor'])
def test_saved_library_import_failure_rolls_back_all_aliases_facts_and_memberships(saved_library_source, tmp_path, failure):
    from modules.youtube.user_schema import import_catalog_library
    source = saved_library_source
    if failure == 'invalid-successor':
        with closing(sqlite3.connect(source)) as connection:
            connection.execute("UPDATE youtube_downloads SET superseded_by=? WHERE id='z'", ('bad\0successor',))
            connection.commit()
    original = source.read_bytes()
    def interrupted(connection):
        if failure == 'visibility-conflict':
            identity = migrations.item_catalog.ensure_item(connection, namespace='youtube.download', legacy_key='z', owner='youtube', item_type='saved-media')
            migrations.item_catalog.record_capture(connection, item_id=identity, namespace='youtube.library', capture_key='z', format_version=1,
                payload=dict(legacy_id='z', listed=False, superseded_by='other'))
        count = import_catalog_library(connection)
        if failure == 'late-exception':
            raise RuntimeError('Interrupted after saved-library import')
        return count
    with pytest.raises(migrations.PreparationError):
        migrations.prepare_essential_candidate(source, tmp_path / 'interrupted',
            catalog_importers=[migrations.CatalogImporter('youtube.saved-library', 1, interrupted)])
    with closing(sqlite3.connect(tmp_path / 'interrupted/essential.sqlite')) as connection:
        for table in ('kv_items', 'kv_legacy_keys', 'kv_item_captures', 'kv_libraries'):
            assert connection.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM youtube_downloads WHERE status='complete'").fetchone()[0] == 9
    assert source.read_bytes() == original


def test_saved_library_current_source_and_job_changes_do_not_rewrite_saved_baseline(saved_library_source, tmp_path):
    prepared = migrations.prepare_essential_candidate(saved_library_source, tmp_path / 'candidate', catalog_importers=[library_importer()])
    with closing(sqlite3.connect(prepared.candidate)) as connection:
        before = youtube_metadata.preserved_library(connection)
        captured = connection.execute('SELECT * FROM kv_item_captures ORDER BY item_id,namespace,capture_key').fetchall()
        connection.execute("UPDATE files_sources SET visible=0,path='Z:/relocated-but-unavailable' WHERE source_id='src-first'")
        connection.execute("UPDATE youtube_downloads SET title='Changed operational title',status='cancelled',superseded_by='new-job'")
        connection.commit()
        assert youtube_metadata.preserved_library(connection) == before
        assert connection.execute('SELECT * FROM kv_item_captures ORDER BY item_id,namespace,capture_key').fetchall() == captured


@pytest.mark.parametrize('q,offset,limit', [(None, 0, 60), ('x' * 257, 0, 60), ('', True, 60), ('', -1, 60),
                                        ('', 2**63, 60), ('', 0, 0), ('', 0, 121), ('', 0, True)])
def test_saved_library_invalid_query_refuses_before_sql(q, offset, limit):
    with closing(sqlite3.connect(':memory:')) as connection, pytest.raises(ValueError, match='query'):
        youtube_metadata.preserved_library(connection, q, offset, limit)


def saved_playback_failure(connection, case, reason='decode', identity=None, suite_home=None):
    return youtube_metadata.log_preserved_playback_failure(connection, identity or case.record['id'], reason,
                                                         suite_home=suite_home or case.directory / 'home')


@pytest.mark.parametrize('factory', [sqlite3.Row, lambda cursor, row: dict(zip([column[0] for column in cursor.description], row))], ids=['row', 'dict'])
def test_preserved_playback_failure_readonly_without_jobs_and_exact_evidence(saved_catalog_media, factory, caplog):
    case = saved_catalog_media
    database = case.directory / 'playback.sqlite'
    with closing(art_catalog(case, database, factory=factory)):
        pass
    original = database.read_bytes()
    files = {path: path.read_bytes() for path in case.root.rglob('*') if path.is_file()}
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = factory
        queries = []
        connection.set_trace_callback(queries.append)
        assert saved_playback_failure(connection, case) == {'status': 'logged'}
        assert connection.total_changes == 0 and connection.row_factory is factory
        assert all(query.lstrip().upper().startswith('SELECT') and 'youtube_downloads' not in query for query in queries)
    entries = [record for record in caplog.records if record.name == 'keivotos.youtube']
    assert len(entries) == 1 and case.record['id'] in entries[0].getMessage()
    assert 'reason=decode' in entries[0].getMessage()
    assert case.record['url'] not in entries[0].getMessage() and case.record['root'] not in entries[0].getMessage()
    assert database.read_bytes() == original and files == {path: path.read_bytes() for path in files}


@pytest.mark.parametrize('reason', ['unsupported_format', 'network', 'decode', 'unsupported_codec', 'playback', 'fullscreen'])
def test_preserved_playback_failure_accepts_existing_fixed_codes(saved_catalog_media, reason, caplog):
    case = saved_catalog_media
    with closing(art_catalog(case)) as connection:
        assert saved_playback_failure(connection, case, reason) == {'status': 'logged'}
    assert 'reason=' + reason in caplog.text


@pytest.mark.parametrize('identity,reason', [('a', 'arbitrary\nclient error'), ('a', None), ('', 'decode'), ('x' * 65, 'decode'), (None, 'decode')])
def test_preserved_playback_failure_refuses_unbounded_input_before_lookup(identity, reason, caplog):
    with closing(sqlite3.connect(':memory:')) as connection, pytest.raises(ValueError, match='diagnostic'):
        youtube_metadata.log_preserved_playback_failure(connection, identity, reason, suite_home=Path('.'))
    assert not caplog.records


@pytest.mark.parametrize('hazard', ['unknown', 'hidden', 'role', 'library', 'private', 'nested', 'manifest', 'capture', 'missing-media'])
def test_preserved_playback_failure_refuses_unavailable_evidence_without_logging(saved_catalog_media, hazard, caplog):
    from files_base.serving import ServeDenied
    case = saved_catalog_media
    with closing(art_catalog(case)) as connection:
        if hazard == 'hidden': connection.execute("UPDATE files_sources SET visible=0")
        elif hazard == 'role': connection.execute("UPDATE files_sources SET role='language'")
        elif hazard == 'library': connection.execute("DELETE FROM kv_library_sources")
        elif hazard == 'nested':
            connection.execute("INSERT INTO files_sources(source_id,path,display_name,role,visible) VALUES('child',?,'Hidden child','files',0)",
                               (str(case.root / case.record['bundle_path']),))
        elif hazard == 'manifest': (case.root / case.record['bundle_path'] / '.keivotos/item.json').write_bytes(b'{}')
        elif hazard == 'capture': connection.execute("UPDATE kv_item_captures SET payload_sha256=?", ('00' * 32,))
        elif hazard == 'missing-media': (case.root / case.record['path']).unlink()
        connection.commit()
        with pytest.raises((ValueError, ServeDenied)):
            saved_playback_failure(connection, case, identity='unknown' if hazard == 'unknown' else None,
                                   suite_home=case.root if hazard == 'private' else None)
    assert not [record for record in caplog.records if record.name == 'keivotos.youtube']


def test_preserved_playback_failure_relocation_optional_art_and_contradictory_jobs(saved_catalog_media):
    import shutil
    case = saved_catalog_media
    with closing(art_catalog(case)) as connection:
        connection.executescript(YOUTUBE_SCHEMA)
        connection.execute("INSERT INTO youtube_downloads(id,url,source_id,root,video,audio,status) VALUES(?,?,?,?,?,?,'failed')",
                           (case.record['id'], case.record['url'], 'src-first', str(case.root), 'best', 'best'))
        connection.commit()
        (case.root / case.record['thumbnail']).unlink()
        relocated = case.directory / 'relocated'
        shutil.copytree(case.root, relocated)
        connection.execute("UPDATE files_sources SET path=?", (str(relocated),))
        connection.commit()
        assert saved_playback_failure(connection, case) == {'status': 'logged'}
        assert connection.execute('SELECT status FROM youtube_downloads').fetchone()[0] == 'failed'
        assert youtube_metadata.preserved_capture(connection, case.record['id'])['payload']['record'] == case.record


def test_preserved_playback_failure_old_flat_unsupported_and_audio_evidence(saved_catalog_media, caplog):
    from types import SimpleNamespace
    case = saved_catalog_media
    folder = 'YouTube/' + case.record['id']
    (case.root / folder).mkdir()
    for filename, video in [('audio.m4a', 'audio'), ('video.mkv', 'best')]:
        (case.root / folder / filename).write_bytes(b'preserved old output')
        record = {key: value for key, value in case.record.items()
                  if key not in (*youtube_metadata.COLUMNS, 'layout_version', 'bundle_path', 'manifest_sha256', 'retry_of')}
        record.update(path=folder + '/' + filename, thumbnail='', video=video)
        old = SimpleNamespace(**dict(case.__dict__, record=record, owners=[record['id']]))
        with closing(art_catalog(old)) as connection:
            assert saved_playback_failure(connection, old, reason='unsupported_format') == {'status': 'logged'}
    assert len([record for record in caplog.records if record.name == 'keivotos.youtube']) == 2


def saved_file_http(app, source_id, relative, range_header=None):
    import asyncio
    from urllib.parse import urlencode
    async def run():
        messages = []
        async def receive(): return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(message): messages.append(message)
        headers = [(b'host', b'test')]
        if range_header is not None: headers.append((b'range', range_header.encode()))
        await app(dict(type='http', asgi=dict(version='3.0', spec_version='2.4'), method='GET', http_version='1.1',
            scheme='http', path='/api/files/file', raw_path=b'/api/files/file',
            query_string=urlencode(dict(source_id=source_id, path=relative)).encode(), headers=headers,
            server=('test', 80), client=('127.0.0.1', 12345)), receive, send)
        start = next(message for message in messages if message['type'] == 'http.response.start')
        return start['status'], dict(start['headers']), b''.join(message.get('body', b'') for message in messages if message['type'] == 'http.response.body')
    return asyncio.run(run())


@pytest.mark.parametrize('range_header,expected', [(None, 200), ('bytes=0-8', 206), ('bytes=-5', 206), ('bytes=99999-', 416)])
def test_catalog_selected_playback_uses_existing_files_http_without_jobs(saved_catalog_media, monkeypatch, range_header, expected):
    from contextlib import contextmanager
    from fastapi import FastAPI
    from modules.youtube import playlists
    from routers import files
    case = saved_catalog_media
    database = case.directory / 'http.sqlite'
    with closing(art_catalog(case, database)) as connection:
        selected, reason = playlists.catalog_video(connection, case.record['id'])
        assert not reason and selected
    @contextmanager
    def connect():
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            yield connection
    monkeypatch.setattr(files, 'get_user_db', connect)
    monkeypatch.setattr(files.config, 'SUITE_HOME', case.directory / 'home')
    app = FastAPI(); app.include_router(files.router)
    original = (case.root / case.record['path']).read_bytes()
    status, headers, body = saved_file_http(app, selected['source_id'], selected['path'], range_header)
    assert status == expected
    if status != 416:
        assert headers[b'x-content-type-options'] == b'nosniff' and headers[b'accept-ranges'] == b'bytes'
        assert headers[b'content-type'].startswith(b'video/webm')
        assert body == (original if range_header is None else original[:9] if range_header == 'bytes=0-8' else original[-5:])
    else:
        assert headers[b'content-range'] == ('bytes */' + str(len(original))).encode()
    assert (case.root / case.record['path']).read_bytes() == original
    with connect() as connection:
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='youtube_downloads'").fetchone()


def test_catalog_playback_failure_never_hashes_media_and_logs_escaped_aliases(saved_catalog_media, monkeypatch, caplog):
    case = saved_catalog_media
    original = hashlib.file_digest
    def digest(stream, algorithm):
        assert str(stream.name).endswith('item.json'), 'Playback diagnostics must not hash whole media/art'
        return original(stream, algorithm)
    with closing(art_catalog(case)) as connection:
        monkeypatch.setattr(hashlib, 'file_digest', digest)
        assert saved_playback_failure(connection, case) == {'status': 'logged'}
    assert len([record for record in caplog.records if record.name == 'keivotos.youtube']) == 1


def test_catalog_playback_failure_escapes_legacy_identity_without_changing_evidence(saved_catalog_media, caplog):
    from types import SimpleNamespace
    case = saved_catalog_media
    identity = 'saved\nentry'
    record = dict(case.record, id=identity)
    alias = SimpleNamespace(**dict(case.__dict__, record=record, owners=[identity, *case.owners]))
    with closing(art_catalog(alias)) as connection:
        assert saved_playback_failure(connection, alias) == {'status': 'logged'}
        assert youtube_metadata.preserved_capture(connection, identity)['payload']['record'] == record
    message, = [record.getMessage() for record in caplog.records if record.name == 'keivotos.youtube']
    assert len(message.splitlines()) == 1 and repr(identity) in message


def test_catalog_playback_failure_fresh_process_uses_no_jobs_workers_or_network(saved_catalog_media):
    case = saved_catalog_media
    database = case.directory / 'offline-playback.sqlite'
    with closing(art_catalog(case, database)):
        pass
    script = '''
import builtins, socket, sqlite3, sys
from pathlib import Path
sys.path.insert(0, 'backend')
original = builtins.__import__
def guarded(name, *args, **kwargs):
    assert not name.startswith(('modules.youtube.jobs', 'modules.youtube.worker', 'yt_dlp')), name
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(AssertionError('network'))
from modules.youtube.metadata import log_preserved_playback_failure
with sqlite3.connect(Path(sys.argv[1]).as_uri() + '?mode=ro', uri=True) as connection:
    connection.row_factory = sqlite3.Row
    assert log_preserved_playback_failure(connection, sys.argv[2], 'decode', suite_home=Path(sys.argv[3])) == {'status':'logged'}
assert not any(name.startswith(('modules.youtube.jobs', 'modules.youtube.worker', 'yt_dlp')) for name in sys.modules)
'''
    subprocess.run([sys.executable, '-c', script, str(database), case.record['id'], str(case.directory / 'home')],
                   cwd=Path(__file__).resolve().parents[1], check=True, timeout=30, capture_output=True)


def publication_catalog(path=':memory:'):
    connection = sqlite3.connect(path)
    migrations.item_catalog.ensure_catalog_schema(connection)
    migrations.item_catalog.ensure_library(connection, namespace='files.source', legacy_key='src-first',
        library_id='src-first', kind='linked', display_name='Published saves')
    return connection


def register_publication(connection, case, **changes):
    from modules.youtube.user_schema import register_catalog_publication
    arguments = dict(record=case.record, legacy_job_ids=case.owners, source_id='src-first',
                     source_root=case.root, forbidden_roots=())
    return register_catalog_publication(connection, **(arguments | changes))


def publication_state(connection):
    factory = connection.row_factory
    try:
        connection.row_factory = None
        return migrations._state(connection)
    finally:
        connection.row_factory = factory


@pytest.mark.parametrize('factory', [sqlite3.Row, lambda cursor, row: dict(zip([col[0] for col in cursor.description], row))])
def test_publication_catalog_registers_exact_saved_facts_without_job_tables(saved_catalog_media, factory):
    case = saved_catalog_media
    database = case.directory / 'publication.sqlite'
    contents = {str(p): p.read_bytes() for p in case.root.rglob('*') if p.is_file()}
    with closing(publication_catalog(database)) as connection:
        connection.row_factory = factory
        statements = []
        connection.set_trace_callback(statements.append)
        connection.execute('BEGIN IMMEDIATE')
        result = register_publication(connection, case, record=dict(case.record, status='merging', progress=95, error='operational'))
        before = publication_state(connection)
        assert register_publication(connection, case) == result
        assert publication_state(connection) == before
        assert connection.in_transaction and connection.row_factory is factory
        assert not any('youtube_downloads' in statement for statement in statements)
        connection.commit()
    original = database.read_bytes()
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        assert youtube_metadata.preserved_capture(connection, case.record['id'])['payload'] == dict(
            legacy_job_ids=case.owners, record=case.record)
        saved = youtube_metadata.preserved_library(connection)
        assert saved['total'] == 1 and saved['items'][0]['id'] == case.record['id']
        assert saved['items'][0]['status'] == 'complete' and 'root' not in saved['items'][0]
        assert set(result['assets']) == {'media', 'thumbnail', 'avatar', 'manifest'}
        for asset in result['assets'].values():
            assert asset['sha256'] == hashlib.sha256((case.root / asset['path']).read_bytes()).hexdigest()
    assert database.read_bytes() == original
    assert contents == {str(p): p.read_bytes() for p in case.root.rglob('*') if p.is_file()}


def test_publication_catalog_matches_existing_import_formats_and_identities(saved_catalog_media):
    from modules.youtube.user_schema import migrate, import_catalog_library
    case = saved_catalog_media
    with closing(publication_catalog()) as imported, closing(publication_catalog()) as published:
        imported.executescript(YOUTUBE_SCHEMA + YOUTUBE_PERSONAL_SCHEMA)
        migrate(imported)
        values = dict(case.record, status='complete')
        imported.execute('INSERT INTO youtube_downloads(' + ','.join(values) + ') VALUES(' + ','.join('?' for _ in values) + ')', tuple(values.values()))
        imported.execute("INSERT INTO youtube_downloads(id,url,source_id,root,video,audio,status) VALUES(?,?,?,?,?,?,'failed')",
            (case.owners[1], *[case.record[name] for name in ('url', 'source_id', 'root', 'video', 'audio')]))
        imported.commit()
        original_jobs = imported.execute('SELECT * FROM youtube_downloads ORDER BY id').fetchall()
        imported.execute('BEGIN IMMEDIATE')
        assert import_catalog_library(imported) == 1
        observed = register_media_assets(imported, case)
        imported.commit()
        published.execute('BEGIN IMMEDIATE')
        assert register_publication(published, case) == observed
        published.commit()
        for table in ('kv_items', 'kv_legacy_keys', 'kv_assets', 'kv_item_assets'):
            cursor = imported.execute('SELECT * FROM ' + table)
            columns = [col[0] for col in cursor.description if col[0] != 'created_at']
            assert imported.execute('SELECT ' + ','.join(columns) + ' FROM ' + table).fetchall() == published.execute('SELECT ' + ','.join(columns) + ' FROM ' + table).fetchall()
        query = 'SELECT item_id,namespace,capture_key,format_version,payload_json,payload_sha256 FROM kv_item_captures ORDER BY namespace,capture_key'
        assert imported.execute(query).fetchall() == published.execute(query).fetchall()
        assert imported.execute('SELECT * FROM youtube_downloads ORDER BY id').fetchall() == original_jobs


@pytest.mark.parametrize('hazard', ['missing-field', 'bad-text', 'nul', 'wrong-owner', 'duplicate-owner',
    'missing-parent', 'wrong-source', 'retired', 'missing-media', 'changed-manifest', 'fenced-root'])
def test_publication_catalog_refuses_invalid_or_unavailable_saves_atomically(saved_catalog_media, hazard):
    case = saved_catalog_media
    record = dict(case.record)
    changes = dict(record=record)
    if hazard == 'missing-field': record.pop('created_at')
    elif hazard == 'bad-text': record['title'] = None
    elif hazard == 'nul': record['id'] += '\0'
    elif hazard == 'wrong-owner': changes['legacy_job_ids'] = list(reversed(case.owners))
    elif hazard == 'duplicate-owner': changes['legacy_job_ids'] = [*case.owners, case.owners[-1]]
    elif hazard == 'missing-parent': changes['legacy_job_ids'] = case.owners[:1]
    elif hazard == 'wrong-source': changes['source_id'] = 'another-source'
    elif hazard == 'retired': record['superseded_by'] = 'replacement'
    elif hazard == 'missing-media': (case.root / case.record['path']).unlink()
    elif hazard == 'changed-manifest': (case.root / case.record['bundle_path'] / '.keivotos/item.json').write_text('{}')
    elif hazard == 'fenced-root': changes['forbidden_roots'] = [case.root]
    with closing(publication_catalog()) as connection:
        before = migrations._state(connection)
        connection.execute('BEGIN IMMEDIATE')
        with pytest.raises((ValueError, ServeDenied)):
            register_publication(connection, case, **changes)
        assert connection.in_transaction and migrations._state(connection) == before


def test_publication_catalog_late_failure_preserves_outer_writes_and_full_retry(saved_catalog_media):
    case = saved_catalog_media
    with closing(publication_catalog()) as connection:
        connection.executescript("CREATE TABLE unrelated(note TEXT); CREATE TRIGGER fail_publication BEFORE INSERT ON kv_item_captures WHEN new.namespace='youtube.library' BEGIN SELECT RAISE(ABORT,'membership interrupted'); END;")
        connection.execute("INSERT INTO unrelated VALUES('pending personal edit')")
        before = migrations._state(connection)
        with pytest.raises(sqlite3.IntegrityError, match='membership interrupted'):
            register_publication(connection, case)
        assert connection.in_transaction and migrations._state(connection) == before
        connection.execute('DROP TRIGGER fail_publication')
        result = register_publication(connection, case)
        assert len(result['assets']) == 4
        connection.rollback()
        assert connection.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 0
        assert connection.execute('SELECT count(*) FROM unrelated').fetchone()[0] == 0


def test_publication_catalog_requires_caller_transaction_and_known_library(saved_catalog_media):
    case = saved_catalog_media
    with closing(publication_catalog()) as connection:
        before = migrations._state(connection)
        with pytest.raises(ValueError, match='caller transaction'):
            register_publication(connection, case)
        assert migrations._state(connection) == before
    with closing(sqlite3.connect(':memory:')) as connection:
        migrations.item_catalog.ensure_catalog_schema(connection)
        connection.execute('BEGIN')
        with pytest.raises(ValueError, match='source library'):
            register_publication(connection, case)
        assert connection.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 0


@pytest.mark.parametrize('changed', ['title', 'bytes'])
def test_publication_catalog_conflicts_preserve_original_registration(saved_catalog_media, changed):
    case = saved_catalog_media
    with closing(publication_catalog()) as connection:
        connection.execute('BEGIN')
        register_publication(connection, case)
        connection.commit()
        before = migrations._state(connection)
        record = dict(case.record)
        if changed == 'title': record['title'] = 'different captured title'
        else:
            path = case.root / record['path']
            path.write_bytes(path.read_bytes() + b'changed after registration')
        connection.execute('BEGIN')
        with pytest.raises(migrations.item_catalog.CatalogConflict):
            register_publication(connection, case, record=record)
        assert migrations._state(connection) == before


def retire_saved(connection, case, **changes):
    from modules.youtube.user_schema import retire_catalog_save
    replacement = dict(case.record, id='c' * 32, retry_of=case.record['id']) | changes
    return retire_catalog_save(connection, case.record['id'], replacement)


def registered_publication(case, path=':memory:'):
    connection = publication_catalog(path)
    connection.execute('BEGIN')
    register_publication(connection, case)
    connection.commit()
    return connection


@pytest.mark.parametrize('factory', [sqlite3.Row, lambda cursor, row: dict(zip([col[0] for col in cursor.description], row))])
def test_retirement_survives_job_free_readonly_restart_without_rewriting_facts(saved_catalog_media, factory):
    case = saved_catalog_media
    database = case.directory / 'retirement.sqlite'
    contents = {str(p): p.read_bytes() for p in case.root.rglob('*') if p.is_file()}
    with closing(registered_publication(case, database)) as connection:
        before = publication_state(connection)
        original = youtube_metadata.preserved_capture(connection, case.record['id'])
        connection.row_factory = factory
        connection.execute('BEGIN')
        result = retire_saved(connection, case)
        assert result == dict(legacy_id=case.record['id'], superseded_by='c' * 32)
        assert retire_saved(connection, case) == result
        assert connection.in_transaction and connection.row_factory is factory
        connection.commit()
        after = publication_state(connection)
        assert {key: value for key, value in before['table_sha256'].items() if key != 'kv_item_captures'} == {
            key: value for key, value in after['table_sha256'].items() if key != 'kv_item_captures'}
        assert youtube_metadata.preserved_capture(connection, case.record['id']) == original
    original_bytes = database.read_bytes()
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = factory
        statements = []
        connection.set_trace_callback(statements.append)
        assert youtube_metadata.preserved_library(connection) == dict(items=[], total=0)
        assert youtube_metadata._preserved_file_capture(connection, 'src-first', case.record['path']) is None
        assert youtube_metadata.preserved_capture(connection, case.record['id']) == original
        assert not any('youtube_downloads' in query for query in statements)
        assert connection.total_changes == 0 and connection.row_factory is factory
    assert database.read_bytes() == original_bytes
    assert contents == {str(p): p.read_bytes() for p in case.root.rglob('*') if p.is_file()}


def test_published_replacements_retire_ancestors_atomically_and_keep_reverse_metadata_unambiguous(saved_catalog_media):
    case = saved_catalog_media
    with closing(registered_publication(case)) as connection:
        facts = youtube_metadata.preserved_capture(connection, case.record['id'])
        previous = case.record['id']
        owners = list(case.owners)
        for successor in ('c' * 32, 'd' * 32):
            connection.execute('BEGIN')
            owners = [successor, *owners]
            record = dict(case.record, id=successor, retry_of=previous)
            register_publication(connection, case, record=record, legacy_job_ids=owners)
            connection.commit()
            assert youtube_metadata.preserved_library(connection)['total'] == 1
            assert youtube_metadata.preserved_library(connection)['items'][0]['id'] == successor
            assert youtube_metadata._preserved_file_capture(connection, 'src-first', record['path'])['payload']['record']['id'] == successor
            previous = successor
        assert youtube_metadata.preserved_capture(connection, case.record['id']) == facts
        assert connection.execute("SELECT count(*) FROM kv_item_captures WHERE namespace='youtube.retirement'").fetchone()[0] == 2
        assert youtube_metadata.preserved_library(connection, 'saved', 1, 1) == dict(items=[], total=1)
        assert youtube_metadata.preserved_library(connection, '%') == dict(items=[], total=0)


@pytest.mark.parametrize('changed', ['id', 'retry_of', 'url', 'source_id', 'root', 'video', 'audio', 'nul', 'type'])
def test_retirement_refuses_incompatible_or_invalid_successors_without_new_rows(saved_catalog_media, changed):
    case = saved_catalog_media
    changes = {changed: 'incompatible'}
    if changed == 'id': changes = dict(id=case.record['id'])
    elif changed == 'nul': changes = dict(id='c\0d')
    elif changed == 'type': changes = dict(id=None)
    with closing(registered_publication(case)) as connection:
        before = publication_state(connection)
        connection.execute('BEGIN')
        with pytest.raises(ValueError): retire_saved(connection, case, **changes)
        assert connection.in_transaction and publication_state(connection) == before
        assert youtube_metadata.preserved_library(connection)['total'] == 1


def test_retirement_requires_transaction_and_preserved_facts_but_not_an_unknown_failed_attempt(saved_catalog_media):
    from modules.youtube.user_schema import retire_catalog_save
    case = saved_catalog_media
    with closing(publication_catalog()) as connection:
        with pytest.raises(ValueError, match='caller transaction'): retire_saved(connection, case)
        connection.execute('BEGIN')
        assert retire_saved(connection, case) is None
        connection.rollback()
        migrations.item_catalog.ensure_item(connection, namespace='youtube.download', legacy_key=case.record['id'], owner='youtube', item_type='saved-media')
        connection.execute('BEGIN')
        with pytest.raises(ValueError, match='Preserve saved facts'): retire_saved(connection, case)
        connection.rollback()
    with closing(media_catalog(case.record, case.owners)) as connection:
        connection.execute('BEGIN')
        with pytest.raises(ValueError, match='Preserve saved membership'): retire_saved(connection, case)


def test_retirement_conflict_rollback_and_corrupt_acknowledgement_preserve_baseline(saved_catalog_media):
    case = saved_catalog_media
    with closing(registered_publication(case)) as connection:
        before = publication_state(connection)
        connection.execute('BEGIN')
        retire_saved(connection, case)
        connection.rollback()
        assert publication_state(connection) == before
        connection.execute('BEGIN')
        retire_saved(connection, case)
        connection.commit()
        retired = publication_state(connection)
        connection.execute('BEGIN')
        with pytest.raises(migrations.item_catalog.CatalogConflict): retire_saved(connection, case, id='d' * 32)
        assert publication_state(connection) == retired
        connection.rollback()
        connection.execute("UPDATE kv_item_captures SET payload_sha256=? WHERE namespace='youtube.retirement'", ('0' * 64,))
        connection.commit()
        connection.execute('BEGIN')
        with pytest.raises(migrations.item_catalog.CatalogConflict): retire_saved(connection, case)
        # Listing is structural validation, not a full hash audit of hidden rows.
        assert youtube_metadata.preserved_library(connection) == dict(items=[], total=0)


@pytest.mark.parametrize('corrupt', ['version', 'json', 'id', 'empty', 'self', 'nul', 'extra', 'orphan', 'owner'])
def test_retirement_queries_reject_malformed_or_orphaned_evidence(saved_catalog_media, corrupt):
    case = saved_catalog_media
    with closing(registered_publication(case)) as connection:
        connection.execute('BEGIN')
        retire_saved(connection, case)
        connection.commit()
        payload = dict(legacy_id=case.record['id'], superseded_by='c' * 32)
        if corrupt == 'version': connection.execute("UPDATE kv_item_captures SET format_version=2 WHERE namespace='youtube.retirement'")
        elif corrupt == 'owner': connection.execute("UPDATE kv_items SET owner='foreign'")
        elif corrupt == 'orphan': connection.execute("UPDATE kv_item_captures SET capture_key='orphan' WHERE namespace='youtube.retirement'")
        elif corrupt == 'json': connection.execute("UPDATE kv_item_captures SET payload_json='{' WHERE namespace='youtube.retirement'")
        else:
            if corrupt == 'id': payload['legacy_id'] = 'foreign'
            elif corrupt == 'empty': payload['superseded_by'] = ''
            elif corrupt == 'self': payload['superseded_by'] = case.record['id']
            elif corrupt == 'nul': payload['superseded_by'] = 'c\0d'
            elif corrupt == 'extra': payload['unknown'] = True
            connection.execute("UPDATE kv_item_captures SET payload_json=? WHERE namespace='youtube.retirement'", (json.dumps(payload),))
        connection.commit()
        before = publication_state(connection)
        with pytest.raises(ValueError, match='retirement'): youtube_metadata.preserved_library(connection)
        with pytest.raises(ValueError, match='retirement'): youtube_metadata._preserved_file_capture(connection, 'src-first', case.record['path'])
        assert publication_state(connection) == before


def test_retirement_publication_conflict_rolls_back_the_entire_replacement(saved_catalog_media):
    case = saved_catalog_media
    with closing(registered_publication(case)) as connection:
        connection.execute('BEGIN')
        retire_saved(connection, case, id='d' * 32)
        connection.commit()
        before = publication_state(connection)
        connection.execute('BEGIN')
        with pytest.raises(migrations.item_catalog.CatalogConflict):
            register_publication(connection, case, record=dict(case.record, id='c' * 32, retry_of=case.record['id']),
                legacy_job_ids=['c' * 32, *case.owners])
        assert connection.in_transaction and publication_state(connection) == before


def test_retirement_conflicting_migration_baseline_is_preserved(saved_catalog_media):
    from modules.youtube.user_schema import retire_catalog_save
    case = saved_catalog_media
    with closing(registered_publication(case)) as connection:
        original = dict(legacy_id=case.record['id'], listed=False, superseded_by='d' * 32)
        payload, digest = migrations.item_catalog._capture_payload(original)
        connection.execute("UPDATE kv_item_captures SET payload_json=?,payload_sha256=? WHERE namespace='youtube.library'", (payload, digest))
        connection.commit()
        before = publication_state(connection)
        connection.execute('BEGIN')
        with pytest.raises(migrations.item_catalog.CatalogConflict): retire_saved(connection, case)
        assert publication_state(connection) == before
        assert retire_saved(connection, case, id='d' * 32)['superseded_by'] == 'd' * 32
        assert youtube_metadata.preserved_library(connection)['total'] == 0


TWITTER_CAPTURE = '20261015T120000000000Z-1234abcd'
TWITTER_LATER_CAPTURE = '20261015T120001000000Z-1234abcd'


def twitter_receipt(identity='one', source_id='src-first', post_id='999', capture_id=TWITTER_CAPTURE):
    return dict(id=identity, request_id='request-' + identity, url='https://x.com/SavedAuthor/status/' + post_id,
        post_id=post_id, source_id=source_id, root='/unavailable/original-root', capture_id=capture_id,
        path=f'captures/{post_id}/{capture_id}/manifest.json', created_at='2026-10-15T12:00:00Z')


def add_twitter_receipt(connection, record, status='complete'):
    values = dict(record, status=status)
    connection.execute('INSERT INTO twitter_downloads(' + ','.join(values) + ') VALUES(' + ','.join('?' for _ in values) + ')', tuple(values.values()))


@pytest.fixture
def twitter_receipt_source(essential_source):
    from modules.twitter.user_schema import SCHEMA
    with closing(sqlite3.connect(essential_source)) as connection:
        connection.executescript(SCHEMA)
        connection.execute('ALTER TABLE twitter_downloads ADD COLUMN preserved_unknown BLOB')
        connection.execute("INSERT INTO twitter_preferences VALUES(1,'src-first')")
        for record in (twitter_receipt(), twitter_receipt('shared'),
                twitter_receipt('later', capture_id=TWITTER_LATER_CAPTURE),
                twitter_receipt('another-source', source_id='src-second'),
                twitter_receipt('another-post', post_id='1000'),
                twitter_receipt('missing-source', source_id='missing-source')):
            add_twitter_receipt(connection, record)
        for status in ('queued', 'running', 'failed', 'cancelled', 'interrupted'):
            add_twitter_receipt(connection, dict(twitter_receipt(status), path='incomplete', capture_id='unfinished'), status)
        connection.execute('UPDATE twitter_downloads SET preserved_unknown=?', (b'unknown original \0\xff',))
        connection.commit()
    return essential_source


def twitter_receipt_importer(callback=None):
    from modules.twitter.user_schema import import_catalog_receipts
    return migrations.CatalogImporter('twitter.saved-receipts', 1, callback or import_catalog_receipts)


def read_twitter_receipt(connection, record=None, **changes):
    from modules.twitter.user_schema import preserved_receipt
    record = record or twitter_receipt()
    arguments = dict(legacy_id=record['id'], source_id=record['source_id'],
                     post_id=record['post_id'], capture_id=record['capture_id'])
    return preserved_receipt(connection, **(arguments | changes))


@pytest.mark.parametrize('factory', [sqlite3.Row, lambda cursor, row: dict(zip([col[0] for col in cursor.description], row))])
def test_twitter_catalog_import_preserves_legacy_and_distinguishes_captures(twitter_receipt_source, tmp_path, factory):
    source = twitter_receipt_source
    original = source.read_bytes()
    with closing(sqlite3.connect(source)) as connection:
        before = migrations._state(connection)
        objects = {(row[0], row[1]) for row in connection.execute('SELECT type,name FROM sqlite_master WHERE sql IS NOT NULL')}
    prepared = migrations.prepare_essential_candidate(source, tmp_path / 'twitter', catalog_importers=[twitter_receipt_importer()])
    assert prepared.imported_items == {'twitter.saved-receipts': 6}
    with closing(sqlite3.connect(prepared.candidate)) as connection:
        assert migrations._state(connection, schema_objects=objects, table_names=set(before['table_rows'])) == before
        assert connection.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 5
        assert connection.execute('SELECT count(*) FROM kv_legacy_keys').fetchone()[0] == 11
        assert connection.execute('SELECT count(*) FROM kv_item_captures').fetchone()[0] == 6
        assert connection.execute('SELECT count(*) FROM kv_assets').fetchone()[0] == 0
        connection.row_factory = factory
        first = read_twitter_receipt(connection)
        assert first['payload'] == dict(record=twitter_receipt())
        assert read_twitter_receipt(connection, twitter_receipt('shared'))['item_id'] == first['item_id']
        for record in (twitter_receipt('later', capture_id=TWITTER_LATER_CAPTURE),
                twitter_receipt('another-source', source_id='src-second'), twitter_receipt('another-post', post_id='1000'),
                twitter_receipt('missing-source', source_id='missing-source')):
            assert read_twitter_receipt(connection, record)['item_id'] != first['item_id']
        assert connection.row_factory is factory
        assert read_twitter_receipt(connection, legacy_id='queued') is None
    assert migrations.validate_essential_candidate(source, prepared.attempt_directory, catalog_importers=[twitter_receipt_importer()]) == prepared
    assert source.read_bytes() == original


def test_twitter_catalog_fresh_attempts_relocation_and_reused_job_ids_keep_capture_identity(twitter_receipt_source, tmp_path):
    source = twitter_receipt_source
    first = migrations.prepare_essential_candidate(source, tmp_path / 'first', catalog_importers=[twitter_receipt_importer()])
    with closing(sqlite3.connect(first.candidate)) as connection:
        original = read_twitter_receipt(connection)
        later = read_twitter_receipt(connection, twitter_receipt('later', capture_id=TWITTER_LATER_CAPTURE))
    saved = first.candidate.read_bytes()
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("UPDATE files_sources SET path='Z:/relocated' WHERE source_id='src-first'")
        connection.execute("UPDATE twitter_downloads SET root='Z:/relocated',url='https://twitter.com/RenamedAuthor/status/999' WHERE id='one'")
        connection.commit()
    relocated = migrations.prepare_essential_candidate(source, tmp_path / 'relocated', catalog_importers=[twitter_receipt_importer()])
    with closing(sqlite3.connect(relocated.candidate)) as connection:
        changed = read_twitter_receipt(connection)
        assert changed['item_id'] == original['item_id']
        assert changed['payload']['record']['root'] == 'Z:/relocated'
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("UPDATE twitter_downloads SET capture_id=?,path=? WHERE id='one'", (TWITTER_LATER_CAPTURE,
            f'captures/999/{TWITTER_LATER_CAPTURE}/manifest.json'))
        connection.commit()
    retried = migrations.prepare_essential_candidate(source, tmp_path / 'retried', catalog_importers=[twitter_receipt_importer()])
    with closing(sqlite3.connect(retried.candidate)) as connection:
        new_capture = read_twitter_receipt(connection, capture_id=TWITTER_LATER_CAPTURE)
        assert new_capture['item_id'] == later['item_id'] != original['item_id']
        assert read_twitter_receipt(connection) is None
    assert first.candidate.read_bytes() == saved


@pytest.mark.parametrize('factory', [sqlite3.Row, lambda cursor, row: dict(zip([col[0] for col in cursor.description], row))])
def test_twitter_catalog_receipt_readonly_restart_needs_no_jobs_or_files(twitter_receipt_source, tmp_path, factory, monkeypatch):
    from modules.twitter import archive
    prepared = migrations.prepare_essential_candidate(twitter_receipt_source, tmp_path / 'candidate', catalog_importers=[twitter_receipt_importer()])
    database = tmp_path / 'no-jobs.sqlite'
    with closing(job_free_library(prepared.candidate, database)): pass
    original = database.read_bytes()
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = factory
        statements = []
        connection.set_trace_callback(statements.append)
        def forbidden(*args, **kwargs): raise AssertionError('Receipt lookup accessed original files')
        with monkeypatch.context() as patch:
            patch.setattr(archive, 'capture', forbidden)
            patch.setattr(Path, 'open', forbidden)
            patch.setattr(Path, 'stat', forbidden)
            patch.setattr(Path, 'resolve', forbidden)
            actual = read_twitter_receipt(connection)
            assert actual['payload']['record'] == twitter_receipt()
            assert read_twitter_receipt(connection, legacy_id='unknown') is None
        assert all(query.lstrip().upper().startswith('SELECT') and 'twitter_downloads' not in query for query in statements)
        assert connection.row_factory is factory and connection.total_changes == 0
        connection.set_trace_callback(None)
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='twitter_downloads'").fetchone()
    assert database.read_bytes() == original


def test_twitter_catalog_owner_failure_rolls_back_and_resumes(twitter_receipt_source, tmp_path):
    from modules.twitter.user_schema import import_catalog_receipts
    def interrupted(connection):
        assert import_catalog_receipts(connection) == 6
        raise RuntimeError('interrupted Twitter receipts')
    original = twitter_receipt_source.read_bytes()
    attempt = tmp_path / 'interrupted'
    with pytest.raises(migrations.PreparationError, match='interrupted Twitter receipts'):
        migrations.prepare_essential_candidate(twitter_receipt_source, attempt, catalog_importers=[twitter_receipt_importer(interrupted)])
    with closing(sqlite3.connect(attempt / 'essential.sqlite')) as connection:
        for table in ('kv_libraries', 'kv_items', 'kv_legacy_keys', 'kv_item_captures'):
            assert connection.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0
        assert connection.execute('SELECT count(*) FROM twitter_downloads').fetchone()[0] == 11
    resumed = migrations.prepare_essential_candidate(twitter_receipt_source, attempt, catalog_importers=[twitter_receipt_importer()])
    assert resumed.imported_items == {'twitter.saved-receipts': 6}
    assert twitter_receipt_source.read_bytes() == original


def test_twitter_catalog_import_exact_retry_and_caller_rollback(twitter_receipt_source):
    from modules.twitter.user_schema import import_catalog_receipts
    with closing(sqlite3.connect(':memory:')) as connection, closing(sqlite3.connect(twitter_receipt_source)) as source:
        source.backup(connection)
        migrations.item_catalog.ensure_catalog_schema(connection)
        connection.execute('BEGIN IMMEDIATE')
        assert import_catalog_receipts(connection) == 6
        registered = publication_state(connection)
        assert import_catalog_receipts(connection) == 6
        assert connection.in_transaction and publication_state(connection) == registered
        connection.rollback()
        assert connection.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 0
        with pytest.raises(ValueError, match='caller transaction'): import_catalog_receipts(connection)


@pytest.mark.parametrize('changed', ['id', 'request_id', 'post_id', 'source_id', 'capture_id', 'path', 'url', 'root', 'created_at'])
def test_twitter_catalog_malformed_completed_receipt_stops_candidate_without_source_writes(twitter_receipt_source, tmp_path, changed):
    values = dict(id='', request_id=None, post_id='1000', source_id='', capture_id='20261315T120000000000Z-1234abcd',
        path='captures/999/../manifest.json', url='https://evil.invalid/status/999', root='bad\0root', created_at='')
    # NOT NULL belongs to the legacy schema; use an empty token for this column.
    value = '' if changed == 'request_id' else values[changed]
    with closing(sqlite3.connect(twitter_receipt_source)) as connection:
        connection.execute('UPDATE twitter_downloads SET ' + changed + '=? WHERE id=?', (value, 'one'))
        connection.commit()
    original = twitter_receipt_source.read_bytes()
    with pytest.raises(migrations.PreparationError):
        migrations.prepare_essential_candidate(twitter_receipt_source, tmp_path / 'bad', catalog_importers=[twitter_receipt_importer()])
    assert twitter_receipt_source.read_bytes() == original
    with closing(sqlite3.connect(tmp_path / 'bad/essential.sqlite')) as connection:
        assert connection.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 0


def test_twitter_catalog_absent_and_minimal_old_owner_schemas(essential_source, tmp_path):
    missing = migrations.prepare_essential_candidate(essential_source, tmp_path / 'absent', catalog_importers=[twitter_receipt_importer()])
    assert missing.imported_items == {'twitter.saved-receipts': 0}
    from modules.twitter.user_schema import CATALOG_RECEIPT_FIELDS
    with closing(sqlite3.connect(essential_source)) as connection:
        connection.execute('CREATE TABLE twitter_downloads(' + ','.join(name + ' TEXT' for name in (*CATALOG_RECEIPT_FIELDS, 'status')) + ')')
        add_twitter_receipt(connection, twitter_receipt())
        connection.commit()
    old = migrations.prepare_essential_candidate(essential_source, tmp_path / 'old', catalog_importers=[twitter_receipt_importer()])
    with closing(sqlite3.connect(old.candidate)) as connection:
        assert read_twitter_receipt(connection)['payload']['record'] == twitter_receipt()
        assert connection.execute("SELECT count(*) FROM pragma_table_info('twitter_downloads')").fetchone()[0] == 10


def test_twitter_catalog_unknown_owner_schema_refuses_even_when_empty(essential_source, tmp_path):
    with closing(sqlite3.connect(essential_source)) as connection:
        connection.execute('CREATE TABLE twitter_downloads(id TEXT,status TEXT)')
        connection.commit()
    original = essential_source.read_bytes()
    with pytest.raises(migrations.PreparationError, match='Unsupported Twitter receipt schema'):
        migrations.prepare_essential_candidate(essential_source, tmp_path / 'unknown', catalog_importers=[twitter_receipt_importer()])
    assert essential_source.read_bytes() == original


@pytest.mark.parametrize('corrupt', ['format', 'hash', 'extra', 'identity', 'owner', 'alias', 'missing-alias', 'missing-facts'])
def test_twitter_catalog_receipt_reader_refuses_corrupt_facts_and_aliases(twitter_receipt_source, tmp_path, corrupt):
    prepared = migrations.prepare_essential_candidate(twitter_receipt_source, tmp_path / 'candidate', catalog_importers=[twitter_receipt_importer()])
    with closing(job_free_library(prepared.candidate)) as connection:
        first = read_twitter_receipt(connection)
        identity = first['item_id']
        if corrupt == 'format': connection.execute('UPDATE kv_item_captures SET format_version=2 WHERE item_id=?', (identity,))
        elif corrupt == 'hash': connection.execute('UPDATE kv_item_captures SET payload_sha256=? WHERE item_id=?', ('0' * 64, identity))
        elif corrupt == 'owner': connection.execute("UPDATE kv_items SET owner='foreign' WHERE id=?", (identity,))
        elif corrupt in ('alias', 'missing-alias'):
            from modules.twitter.user_schema import _catalog_key
            alias = _catalog_key('src-first', '999', TWITTER_CAPTURE, 'one')
            if corrupt == 'alias':
                other = read_twitter_receipt(connection, twitter_receipt('later', capture_id=TWITTER_LATER_CAPTURE))['item_id']
                connection.execute("UPDATE kv_legacy_keys SET item_id=? WHERE namespace='twitter.download-capture' AND legacy_key=?", (other, alias))
            else: connection.execute("UPDATE kv_legacy_keys SET legacy_key='displaced' WHERE namespace='twitter.download-capture' AND legacy_key=?", (alias,))
        elif corrupt == 'missing-facts': connection.execute("UPDATE kv_item_captures SET capture_key='displaced' WHERE item_id=? AND capture_key='one'", (identity,))
        else:
            payload = first['payload']
            if corrupt == 'extra': payload['unknown'] = True
            else: payload['record']['id'] = 'foreign'
            text, digest = migrations.item_catalog._capture_payload(payload)
            connection.execute("UPDATE kv_item_captures SET payload_json=?,payload_sha256=? WHERE item_id=? AND capture_key='one'", (text, digest, identity))
        connection.commit()
        before = publication_state(connection)
        with pytest.raises(ValueError): read_twitter_receipt(connection)
        assert publication_state(connection) == before


def test_twitter_catalog_same_job_can_preserve_receipts_for_multiple_captures(twitter_receipt_source):
    from modules.twitter.user_schema import import_catalog_receipts
    with closing(sqlite3.connect(':memory:')) as connection, closing(sqlite3.connect(twitter_receipt_source)) as source:
        source.backup(connection)
        migrations.item_catalog.ensure_catalog_schema(connection)
        connection.execute('BEGIN')
        import_catalog_receipts(connection)
        connection.commit()
        old = read_twitter_receipt(connection)
        connection.execute("UPDATE twitter_downloads SET capture_id=?,path=? WHERE id='one'", (TWITTER_LATER_CAPTURE,
            f'captures/999/{TWITTER_LATER_CAPTURE}/manifest.json'))
        connection.commit()
        connection.execute('BEGIN')
        import_catalog_receipts(connection)
        connection.commit()
        newer = read_twitter_receipt(connection, capture_id=TWITTER_LATER_CAPTURE)
        assert newer['item_id'] != old['item_id']
        assert read_twitter_receipt(connection) == old
        assert connection.execute("SELECT count(*) FROM kv_item_captures WHERE namespace='twitter.download' AND capture_key='one'").fetchone()[0] == 2


def test_twitter_catalog_prepared_source_and_import_configuration_cannot_change(twitter_receipt_source, tmp_path):
    importer = twitter_receipt_importer()
    prepared = migrations.prepare_essential_candidate(twitter_receipt_source, tmp_path / 'candidate', catalog_importers=[importer])
    original = prepared.candidate.read_bytes()
    assert migrations.prepare_essential_candidate(twitter_receipt_source, prepared.attempt_directory, catalog_importers=[importer]) == prepared
    for configuration in ([], [migrations.CatalogImporter(importer.name, 2, importer.import_records)]):
        with pytest.raises(migrations.PreparationError, match='configuration changed'):
            migrations.validate_essential_candidate(twitter_receipt_source, prepared.attempt_directory, catalog_importers=configuration)
    with closing(sqlite3.connect(twitter_receipt_source)) as connection:
        connection.execute("UPDATE twitter_downloads SET root='moved' WHERE id='one'"); connection.commit()
    with pytest.raises(migrations.PreparationError):
        migrations.validate_essential_candidate(twitter_receipt_source, prepared.attempt_directory, catalog_importers=[importer])
    assert prepared.candidate.read_bytes() == original


def test_twitter_catalog_direct_import_does_not_resolve_or_read_media(twitter_receipt_source, monkeypatch):
    from modules.twitter.user_schema import import_catalog_receipts
    from modules.twitter import archive
    with closing(sqlite3.connect(':memory:')) as connection, closing(sqlite3.connect(twitter_receipt_source)) as source:
        source.backup(connection)
        migrations.item_catalog.ensure_catalog_schema(connection)
        connection.execute('BEGIN')
        def forbidden(*args, **kwargs): raise AssertionError('Receipt import accessed files')
        with monkeypatch.context() as patch:
            patch.setattr(archive, 'capture', forbidden)
            for name in ('open', 'resolve', 'stat'): patch.setattr(Path, name, forbidden)
            assert import_catalog_receipts(connection) == 6
        assert connection.in_transaction
        assert connection.execute('SELECT count(*) FROM kv_assets').fetchone()[0] == 0


@pytest.mark.parametrize('conflict', ['identity', 'facts'])
def test_twitter_catalog_late_conflicts_preserve_all_existing_rows(twitter_receipt_source, conflict):
    from modules.twitter.user_schema import _catalog_key, import_catalog_receipts
    with closing(sqlite3.connect(':memory:')) as connection, closing(sqlite3.connect(twitter_receipt_source)) as source:
        source.backup(connection)
        migrations.item_catalog.ensure_catalog_schema(connection)
        if conflict == 'identity':
            key = _catalog_key('src-first', '999', TWITTER_CAPTURE)
            migrations.item_catalog.ensure_item(connection, namespace='twitter.capture', legacy_key=key,
                owner='foreign', item_type='saved-post')
        else:
            connection.execute('BEGIN')
            import_catalog_receipts(connection)
            connection.commit()
            connection.execute("UPDATE twitter_downloads SET root='changed' WHERE id='one'"); connection.commit()
        before = publication_state(connection)
        with pytest.raises(migrations.item_catalog.CatalogConflict):
            with connection:
                connection.execute('BEGIN')
                import_catalog_receipts(connection)
        assert publication_state(connection) == before


def test_twitter_catalog_and_youtube_bridges_share_candidate_without_cross_owner_imports(twitter_receipt_source, tmp_path):
    with closing(sqlite3.connect(twitter_receipt_source)) as connection:
        connection.executescript(YOUTUBE_SCHEMA + YOUTUBE_PERSONAL_SCHEMA)
        connection.execute("INSERT INTO youtube_downloads(id,url,source_id,root,video,audio,status) VALUES('youtube','https://www.youtube.com/watch?v=abcdefghijk','src-first','unavailable','best','best','complete')")
        connection.commit()
    result = migrations.prepare_essential_candidate(twitter_receipt_source, tmp_path / 'combined',
        catalog_importers=[youtube_importer(), twitter_receipt_importer()])
    assert result.imported_items == {'twitter.saved-receipts': 6, 'youtube.saved-media': 1}
    with closing(sqlite3.connect(result.candidate)) as connection:
        assert connection.execute('SELECT owner,count(*) FROM kv_items GROUP BY owner ORDER BY owner').fetchall() == [('twitter', 5), ('youtube', 1)]
        assert read_twitter_receipt(connection)['payload']['record'] == twitter_receipt()


@pytest.mark.parametrize('identity', [None, True, [], '', 'bad\0id', 'x' * 4097])
def test_twitter_catalog_invalid_receipt_identity_refuses_before_database_queries(identity):
    with closing(sqlite3.connect(':memory:')) as connection:
        with pytest.raises(ValueError): read_twitter_receipt(connection, legacy_id=identity)


def test_twitter_catalog_fresh_receipt_reader_never_imports_runtime_or_network(twitter_receipt_source, tmp_path):
    prepared = migrations.prepare_essential_candidate(twitter_receipt_source, tmp_path / 'candidate', catalog_importers=[twitter_receipt_importer()])
    database = tmp_path / 'no-jobs.sqlite'
    with closing(job_free_library(prepared.candidate, database)): pass
    code = '''import builtins,socket,sqlite3,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
original=builtins.__import__
def guarded(name,*args,**kwargs):
    if name in ('config','database','runtime_logging') or name.startswith(('modules.twitter.jobs','modules.twitter.worker','modules.twitter.acquisition','yt_dlp')):
        raise AssertionError('runtime import: '+name)
    return original(name,*args,**kwargs)
builtins.__import__=guarded
socket.create_connection=lambda *a,**k: (_ for _ in ()).throw(AssertionError('network'))
from modules.twitter.user_schema import preserved_receipt
with sqlite3.connect(Path(sys.argv[2]).as_uri()+'?mode=ro',uri=True) as connection:
    actual=preserved_receipt(connection,'one',source_id='src-first',post_id='999',capture_id=sys.argv[3])
    assert actual['payload']['record']['root']=='/unavailable/original-root'
assert not any(name.startswith(('modules.twitter.jobs','modules.twitter.worker','modules.twitter.acquisition','yt_dlp')) for name in sys.modules)
'''
    options = dict(check=True, capture_output=True, timeout=30)
    if os.name == 'nt': options['creationflags'] = subprocess.CREATE_NO_WINDOW
    subprocess.run([sys.executable, '-c', code, str(Path(__file__).resolve().parents[1] / 'backend'), str(database), TWITTER_CAPTURE], **options)


@pytest.fixture
def files_essential_source(essential_source):
    from files_base import annotations
    from user_schema import ensure_suite_user_schema
    from suite_modules import SUITE_MODULES_SCHEMA
    with closing(sqlite3.connect(essential_source)) as db:
        db.row_factory = sqlite3.Row
        ensure_suite_user_schema(db)
        db.executescript(SUITE_MODULES_SCHEMA)
        annotations.ensure_annotations_schema(db)
        db.executemany('INSERT INTO user_settings(key,value) VALUES(?,?)',
                       [('profile_name', '사용자'), ('future_setting', '{"preserve":true}')])
        db.executemany('INSERT INTO suite_enabled_modules(module) VALUES(?)', [('youtube',), ('unknown_future_owner',)])
        first = annotations.upsert_annotation(db, subject_kind='file', content_hash='a' * 32,
            source_id='src-first', relative_path='old-name.zip', description='한글 ' * 100000,
            author='Exact author', extra_info='Original extra', details=[{'label':'手動','value':'Own value'}], metadata_applied=True)
        folder = annotations.upsert_annotation(db, subject_kind='dir', source_id='src-first', relative_path='Same name', description='First folder')
        annotations.upsert_annotation(db, subject_kind='dir', source_id='src-hidden', relative_path='Same name', description='Different root')
        annotations.upsert_annotation(db, subject_kind='dir', source_id='forgotten-source', relative_path='Lost folder', description='Keep when unavailable')
        annotations.upsert_annotation(db, subject_kind='file', content_hash='b' * 32,
            source_id='src-first', relative_path='cleared.mp4', metadata_applied=True)
        annotations.set_links(db, first.id, [dict(url='https://example.invalid/original', label='Saved source', kind='source'),
                                           dict(url='https://example.invalid/author', label='Author', kind='author')])
        for note, digest, name in [(first, 'c' * 32, 'one.png'), (folder, 'c' * 32, 'same-bytes.png'), (first, 'd' * 32, 'two.png')]:
            annotations.add_attachment(db, note.id, content_hash=digest, file_name=name,
                media_type='image/png', size=21, width=70, height=50, caption='Exact caption', stored_root='/unavailable/old-store')
        db.execute('UPDATE files_annotation_attachments SET position=0,is_cover=1')
        db.execute("UPDATE sqlite_sequence SET seq=77 WHERE name='files_annotations'")
        db.execute('ALTER TABLE files_annotations ADD COLUMN future_blob BLOB')
        db.execute('UPDATE files_annotations SET future_blob=?', (b'\0\xffunknown',))
        db.commit()
    return essential_source


def files_essential_importer(callback=None):
    from files_base.annotations import import_catalog_records
    return migrations.CatalogImporter('files.annotations', 1, callback or import_catalog_records)


def files_essential_connection(source):
    db = sqlite3.connect(':memory:')
    with closing(sqlite3.connect(source)) as original:
        original.backup(db)
    migrations.item_catalog.ensure_catalog_schema(db)
    return db


def files_essential_identities(db):
    original_factory = db.row_factory
    db.row_factory = None
    try:
        return {table: db.execute('SELECT ' + columns + ' FROM ' + table + ' ORDER BY 1,2').fetchall()
                for table, columns in {
                    'kv_items':'id,owner,item_type', 'kv_legacy_keys':'namespace,legacy_key,item_id',
                    'kv_assets':'id,media_type,byte_size,hash_algorithm,content_hash',
                    'kv_item_assets':'item_id,asset_id,role,position',
                    'kv_item_captures':'item_id,namespace,capture_key,format_version,payload_json,payload_sha256',
                }.items()}
    finally:
        db.row_factory = original_factory


@pytest.mark.parametrize('factory', [sqlite3.Row, 'dict'])
def test_files_essential_candidate_preserves_actual_notes_settings_and_attachment_references(files_essential_source, tmp_path, monkeypatch, factory):
    from contextlib import contextmanager
    from database_connections import _row_factory
    from files_base import annotations
    from routers import user_settings
    from suite_modules import enabled_ids, set_enabled
    from models import UserSettingUpdate
    source = files_essential_source
    before_bytes = source.read_bytes()
    with closing(sqlite3.connect(source)) as db:
        before = publication_state(db)
        objects = {(row[0], row[1]) for row in db.execute('SELECT type,name FROM sqlite_master WHERE sql IS NOT NULL')}
    importer = files_essential_importer()
    prepared = migrations.prepare_essential_candidate(source, tmp_path / 'files', catalog_importers=[importer])
    assert prepared.imported_items == {'files.annotations': 5} and prepared.registered_libraries == 2
    with closing(sqlite3.connect(prepared.candidate)) as db:
        assert migrations._state(db, schema_objects=objects, table_names=set(before['table_rows'])) == before
        db.row_factory = _row_factory if factory == 'dict' else sqlite3.Row
        note = annotations.get_annotation(db, content_hash='a' * 32, source_id='different-root', relative_path='renamed.zip')
        assert note.id == 1 and note.description == ('한글 ' * 100000).strip() and note.author == 'Exact author'
        assert note.details == [{'label':'手動','value':'Own value'}] and note.metadata_applied
        assert [link.kind for link in note.links] == ['source', 'author']
        assert [value.id for value in note.attachments] == [1, 3]
        assert all(value.caption == 'Exact caption' and value.is_cover for value in note.attachments)
        assert annotations.get_attachment(db, 1).stored_root == '/unavailable/old-store'
        assert annotations.get_annotation(db, source_id='src-first', relative_path='Same name').id != annotations.get_annotation(db, source_id='src-hidden', relative_path='Same name').id
        assert annotations.get_annotation(db, source_id='forgotten-source', relative_path='Lost folder').description == 'Keep when unavailable'
        assert 'b' * 32 not in annotations.annotated_keys(db)[0]
        assert enabled_ids(db) == {'youtube', 'unknown_future_owner'}
        assert db.execute("SELECT seq FROM sqlite_sequence WHERE name='files_annotations'").fetchone()['seq'] == 77
        catalog = migrations.item_catalog.item_for_legacy(db, namespace='files.annotation', legacy_key='1')
        assert catalog['owner'] == 'files' and catalog['item_type'] == 'annotation'
        assert db.execute('SELECT count(*) AS n FROM kv_items').fetchone()['n'] == 5
        assert db.execute('SELECT count(*) AS n FROM kv_assets').fetchone()['n'] == 3
        assert db.execute('SELECT count(DISTINCT content_hash) AS n FROM kv_assets').fetchone()['n'] == 2
    assert migrations.validate_essential_candidate(source, prepared.attempt_directory, catalog_importers=[importer]) == prepared
    @contextmanager
    def candidate_connection():
        with closing(sqlite3.connect(prepared.candidate)) as db:
            db.row_factory = _row_factory if factory == 'dict' else sqlite3.Row
            yield db
    monkeypatch.setattr(user_settings, 'get_user_db', candidate_connection)
    assert user_settings.get_user_setting('profile_name').value == '사용자'
    assert user_settings.put_user_setting('profile_name', UserSettingUpdate(value='Changed name')).value == 'Changed name'
    with candidate_connection() as db:
        set_enabled(db, 'youtube', False)
        assert enabled_ids(db) == {'unknown_future_owner'}
        annotations.upsert_annotation(db, subject_kind='file', content_hash='a' * 32,
            source_id='new-location', relative_path='new-name.zip', description='Edited on the candidate')
        assert annotations.get_annotation(db, content_hash='a' * 32, source_id='src-first', relative_path='old-name.zip').id == 1
        assert db.execute("SELECT value FROM user_settings WHERE key='future_setting'").fetchone()['value'] == '{"preserve":true}'
    assert source.read_bytes() == before_bytes
    with pytest.raises(migrations.PreparationError, match='candidate changed'):
        migrations.validate_essential_candidate(source, prepared.attempt_directory, catalog_importers=[importer])


def test_files_essential_fresh_attempts_relocation_and_mutable_display_facts_keep_identities(files_essential_source, tmp_path):
    source = files_essential_source
    first = migrations.prepare_essential_candidate(source, tmp_path / 'first', catalog_importers=[files_essential_importer()])
    with closing(sqlite3.connect(first.candidate)) as db:
        expected = files_essential_identities(db)
    with closing(sqlite3.connect(source)) as db:
        db.execute("UPDATE files_sources SET path='/another/unavailable/location',display_name='Renamed' WHERE source_id='src-first'")
        db.execute("UPDATE files_annotations SET source_id='new-location',relative_path='renamed.zip',description='Edited' WHERE id=1")
        db.execute("UPDATE files_annotation_attachments SET stored_root='/relocated/store',caption='New caption',position=99 WHERE id=1")
        db.commit()
    second = migrations.prepare_essential_candidate(source, tmp_path / 'second', catalog_importers=[files_essential_importer()])
    with closing(sqlite3.connect(second.candidate)) as db:
        assert files_essential_identities(db) == expected
    with pytest.raises(migrations.PreparationError, match='Source changed'):
        migrations.validate_essential_candidate(source, first.attempt_directory, catalog_importers=[files_essential_importer()])


@pytest.mark.parametrize('factory', [None, sqlite3.Row, 'dict'])
def test_files_essential_direct_import_is_exact_atomic_and_keeps_caller_transaction(files_essential_source, factory):
    from database_connections import _row_factory
    from files_base.annotations import import_catalog_records
    with closing(files_essential_connection(files_essential_source)) as db:
        db.row_factory = _row_factory if factory == 'dict' else factory
        before = publication_state(db)
        db.execute('BEGIN')
        assert import_catalog_records(db) == 5 and db.in_transaction
        assert db.row_factory == (_row_factory if factory == 'dict' else factory)
        identities = files_essential_identities(db)
        assert import_catalog_records(db) == 5 and files_essential_identities(db) == identities
        db.rollback()
        assert publication_state(db) == before
        db.execute('BEGIN')
        assert import_catalog_records(db) == 5
        db.commit()
        with pytest.raises(ValueError, match='caller transaction'): import_catalog_records(db)


@pytest.mark.parametrize('failure', ['late-asset', 'late-capture', 'late-alias'])
def test_files_essential_late_failure_rolls_back_aliases_assets_and_preserves_pending_caller_writes(files_essential_source, monkeypatch, failure):
    from files_base.annotations import import_catalog_records
    catalog = migrations.item_catalog
    with closing(files_essential_connection(files_essential_source)) as db:
        if failure == 'late-alias':
            catalog.ensure_item(db, namespace='files.annotation', legacy_key='5', owner='foreign', item_type='other')
        before = files_essential_identities(db)
        target = 'record_asset' if failure == 'late-asset' else 'record_capture'
        original = getattr(catalog, target)
        calls = 0
        def fail(*a, **kw):
            nonlocal calls
            value = original(*a, **kw)
            calls += 1
            if calls == (3 if failure == 'late-asset' else 8): raise InterruptedError('late import interruption')
            return value
        if failure != 'late-alias': monkeypatch.setattr(catalog, target, fail)
        db.execute('BEGIN')
        db.execute("INSERT INTO user_settings(key,value) VALUES('pending','keep caller write')")
        with pytest.raises((ValueError, InterruptedError)): import_catalog_records(db)
        assert db.in_transaction and files_essential_identities(db) == before
        assert db.execute("SELECT value FROM user_settings WHERE key='pending'").fetchone()[0] == 'keep caller write'
        db.rollback()


@pytest.mark.parametrize('field,value', [('subject_kind','other'), ('content_hash',None), ('source_id',None), ('relative_path',None), ('id',0)])
def test_files_essential_malformed_annotation_stops_candidate_without_source_changes(files_essential_source, tmp_path, field, value):
    source = files_essential_source
    with closing(sqlite3.connect(source)) as db:
        if field in ('source_id', 'relative_path'):
            value = 'bad\0identity'
        db.execute('UPDATE files_annotations SET ' + field + '=? WHERE id=1', (value,))
        db.commit()
    original = source.read_bytes()
    with pytest.raises(migrations.PreparationError):
        migrations.prepare_essential_candidate(source, tmp_path / 'bad', catalog_importers=[files_essential_importer()])
    assert source.read_bytes() == original
    with closing(sqlite3.connect(tmp_path / 'bad/essential.sqlite')) as db:
        assert db.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 0


@pytest.mark.parametrize('field,value', [('content_hash','invalid'), ('size',-1), ('media_type',''), ('id',0)])
def test_files_essential_malformed_attachment_refuses_without_partial_catalog(files_essential_source, field, value):
    from files_base.annotations import import_catalog_records
    with closing(files_essential_connection(files_essential_source)) as db:
        db.execute('UPDATE files_annotation_attachments SET ' + field + '=? WHERE id=3', (value,))
        db.commit()
        before = publication_state(db)
        db.execute('BEGIN')
        with pytest.raises(ValueError): import_catalog_records(db)
        assert db.in_transaction and publication_state(db) == before


@pytest.mark.parametrize('table', ['files_annotation_links', 'files_annotation_attachments'])
def test_files_essential_orphan_relationships_are_preserved_and_refused(files_essential_source, table):
    from files_base.annotations import import_catalog_records
    with closing(files_essential_connection(files_essential_source)) as db:
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('UPDATE ' + table + ' SET annotation_id=999 WHERE id=1')
        db.commit()
        before = publication_state(db)
        db.execute('BEGIN')
        with pytest.raises(ValueError, match='unknown annotation'): import_catalog_records(db)
        assert publication_state(db) == before


@pytest.mark.parametrize('change', ['file-hash', 'folder-path', 'asset-hash', 'asset-size'])
def test_files_essential_reused_identity_cannot_replace_existing_facts(files_essential_source, change):
    from files_base.annotations import import_catalog_records
    with closing(files_essential_connection(files_essential_source)) as db:
        db.execute('BEGIN')
        import_catalog_records(db)
        db.commit()
        if change == 'file-hash': db.execute("UPDATE files_annotations SET content_hash=? WHERE id=1", ('f' * 32,))
        if change == 'folder-path': db.execute("UPDATE files_annotations SET relative_path='Different folder' WHERE id=2")
        if change == 'asset-hash': db.execute("UPDATE files_annotation_attachments SET content_hash=? WHERE id=3", ('e' * 32,))
        if change == 'asset-size': db.execute('UPDATE files_annotation_attachments SET size=22 WHERE id=3')
        db.commit()
        before = publication_state(db)
        db.execute('BEGIN')
        with pytest.raises(migrations.item_catalog.CatalogConflict): import_catalog_records(db)
        assert publication_state(db) == before


@pytest.mark.parametrize('schema,expected', [('',0), ("CREATE TABLE files_annotations(id INTEGER PRIMARY KEY,subject_kind TEXT,content_hash TEXT,source_id TEXT,relative_path TEXT);INSERT INTO files_annotations VALUES(1,'dir',NULL,'lost','folder');",1)])
def test_files_essential_absent_and_old_minimal_owner_schema(schema, expected):
    from files_base.annotations import import_catalog_records
    with closing(sqlite3.connect(':memory:')) as db:
        db.executescript(schema)
        migrations.item_catalog.ensure_catalog_schema(db)
        db.execute('BEGIN')
        assert import_catalog_records(db) == expected


@pytest.mark.parametrize('schema', ['CREATE TABLE files_annotations(id INTEGER PRIMARY KEY);',
    'CREATE TABLE files_annotation_attachments(id INTEGER PRIMARY KEY,annotation_id INTEGER);',
    'CREATE TABLE files_annotations(id INTEGER,subject_kind TEXT,content_hash TEXT,source_id TEXT,relative_path TEXT);'
    "INSERT INTO files_annotations VALUES(1,'dir',NULL,'a','b'),(1,'dir',NULL,'a','b');",
    'CREATE TABLE files_annotations(id INTEGER PRIMARY KEY,subject_kind TEXT,content_hash TEXT,source_id TEXT,relative_path TEXT);'
    'CREATE TABLE files_annotation_links(id INTEGER PRIMARY KEY);'])
def test_files_essential_unknown_or_duplicate_owner_schema_refuses(schema):
    from files_base.annotations import import_catalog_records
    with closing(sqlite3.connect(':memory:')) as db:
        db.executescript(schema)
        migrations.item_catalog.ensure_catalog_schema(db)
        db.execute('BEGIN')
        with pytest.raises(ValueError): import_catalog_records(db)
        assert db.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 0


def test_files_essential_import_never_resolves_originals_or_stored_roots(files_essential_source, monkeypatch):
    from files_base.annotations import import_catalog_records
    import socket
    with closing(files_essential_connection(files_essential_source)) as db:
        def blocked(*a, **kw): raise AssertionError('No original, path or network access during record import')
        with monkeypatch.context() as scoped:
            for name in ('open', 'stat', 'resolve'): scoped.setattr(Path, name, blocked)
            scoped.setattr(socket, 'create_connection', blocked)
            db.execute('BEGIN')
            assert import_catalog_records(db) == 5
            db.commit()


def test_files_essential_snapshot_roundtrip_preserves_catalog_and_editable_owner_rows(files_essential_source, tmp_path):
    from files_base import annotations
    prepared = migrations.prepare_essential_candidate(files_essential_source, tmp_path / 'prepared', catalog_importers=[files_essential_importer()])
    restored = tmp_path / 'restored.sqlite'
    original = prepared.candidate.read_bytes()
    migrations.snapshot_user_database(prepared.candidate, restored)
    assert prepared.candidate.read_bytes() == original
    with closing(sqlite3.connect(prepared.candidate)) as before, closing(sqlite3.connect(restored)) as db:
        assert publication_state(db) == publication_state(before)
        db.row_factory = sqlite3.Row
        assert annotations.get_annotation(db, content_hash='a' * 32, source_id='any', relative_path='any').id == 1
        assert annotations.get_attachment(db, 2).file_name == 'same-bytes.png'
        edited = annotations.upsert_annotation(db, subject_kind='dir', source_id='src-first', relative_path='Same name', description='Restored edit')
        assert edited.id == 2 and edited.description == 'Restored edit'
        assert files_essential_identities(db) == files_essential_identities(before)


def test_files_essential_fresh_job_free_import_never_loads_runtime_or_optional_owners(tmp_path):
    code = r'''
import builtins, sqlite3, sys
sys.path.insert(0,sys.argv[1])
original=builtins.__import__
def guarded(name,*a,**kw):
    if name in ('config','database','lifecycle','suite_modules') or name.startswith(('modules.','yt_dlp','requests','urllib.request')):
        raise AssertionError('Unexpected runtime/owner/network import: '+name)
    return original(name,*a,**kw)
builtins.__import__=guarded
from files_base.annotations import import_catalog_records, FILES_ANNOTATIONS_SCHEMA
from services.item_catalog import ensure_catalog_schema
with sqlite3.connect(':memory:') as db:
    db.executescript(FILES_ANNOTATIONS_SCHEMA)
    db.execute("INSERT INTO files_annotations(subject_kind,source_id,relative_path) VALUES('dir','unknown','folder')")
    db.commit()
    ensure_catalog_schema(db)
    db.execute('BEGIN')
    assert import_catalog_records(db)==1
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='twitter_downloads'").fetchone()
    db.commit()
'''
    options = dict(check=True, capture_output=True, timeout=30)
    if os.name == 'nt': options['creationflags'] = subprocess.CREATE_NO_WINDOW
    subprocess.run([sys.executable, '-c', code, str(Path(__file__).resolve().parents[1] / 'backend')], **options)


@pytest.fixture
def danbooru_essential_source(files_essential_source, tmp_path):
    from modules.danbooru import user_schema
    with closing(sqlite3.connect(files_essential_source)) as db:
        db.row_factory = sqlite3.Row
        db.executescript(user_schema.USER_SCHEMA)
        user_schema.migrate_user_tables(db, tmp_path / 'absent-index.sqlite', tmp_path / 'absent-media')
        db.executemany('INSERT INTO favorites(file_id,file_path,local_md5,pinned_at) VALUES(?,?,?,?)',
            [(11,'/old/a.png','a'*32,'pinned'), (12,'/old/b.png','a'*32,None),
             (13,None,'b'*32,None), (14,None,None,None)])
        db.executemany('INSERT INTO collections(id,name,description,pinned_at) VALUES(?,?,?,?)',
            [(1,'Same title','First collection','pinned'), (2,'Same title','Second collection',None)])
        db.executemany('INSERT INTO collection_items(collection_id,file_id,file_path,local_md5) VALUES(?,?,?,?)',
            [(1,11,'/old/conflicting.png','c'*32), (1,15,None,'a'*32), (2,11,'/old/a.png','a'*32)])
        db.executemany('INSERT INTO favorite_tags(tag_name,tag_category) VALUES(?,?)', [('same_tag','character'),('same_tag','general')])
        db.executemany('INSERT INTO favorite_tag_combos(name,tag_key,tags_json) VALUES(?,?,?)',
            [('Same title','a\nb','["a","b"]'),('Same title','b\nc','["b","c"]')])
        db.execute("INSERT INTO blacklist_tags(tag_name) VALUES('kept_blacklist')")
        db.executemany('INSERT INTO user_image_tags(file_id,file_path,local_md5,tag_category,tag_name) VALUES(?,?,?,?,?)',
            [(11,'/old/a.png','a'*32,'general','정확한 태그'),(99,None,'a'*32,'character','another_tag')])
        db.executemany('INSERT INTO image_views(file_id,file_path,local_md5,view_count,heart_spam_count) VALUES(?,?,?,?,?)',
            [(11,'/old/a.png','a'*32,21,100),(99,None,'a'*32,3,4)])
        db.execute('INSERT INTO tag_wiki_cache(tag_name,title,body) VALUES(?,?,?)', ('saved_wiki','Kept title','韓国語'*100000))
        db.executemany('INSERT INTO artist_follows(tag_name,notes) VALUES(?,?)', [('author_a','Keep notes'),('author_b','Keep more notes')])
        db.executemany('INSERT INTO artist_follow_posts(tag_name,danbooru_post_id,source,seen_at) VALUES(?,?,?,?)',
                       [('author_a',333,'danbooru','seen'),('author_b',333,'danbooru',None)])
        db.executemany('INSERT INTO artist_profile_assets(tag_name,platform,asset_kind,source_profile_url,source_url,file_path,content_hash,width,height) '
            'VALUES(?,?,?,?,?,?,?,?,?)', [('author_a','twitter','avatar','https://example.invalid/a','https://example.invalid/a1','/unavailable/one.png','a'*64,64,64),
             ('author_a','twitter','avatar','https://example.invalid/a','https://example.invalid/a2','/unavailable/two.png','b'*64,64,64),
             ('author_b','pixiv','avatar','https://example.invalid/b','https://example.invalid/b1','/unavailable/three.png','a'*64,64,64)])
        db.executemany('INSERT INTO tag_removals(file_id,file_path,local_md5,removed_tags_json) VALUES(?,?,?,?)',
                       [(11,'/old/a.png','a'*32,'["old_tag"]'),(None,None,None,'["unknown_subject"]')])
        db.executemany('INSERT INTO registered_folders(name,path,root_id,display_name) VALUES(?,?,?,?)',
            [('old-folder','/unavailable/first','root-fixed','Same title'),
             ('other-folder','/unavailable/second','root-other','Same title'),('legacy-folder',None,None,'Legacy')])
        db.execute('ALTER TABLE artist_profile_assets ADD COLUMN future_blob BLOB')
        db.execute('UPDATE artist_profile_assets SET future_blob=?', (b'\0\xfforiginal',))
        db.execute("UPDATE sqlite_sequence SET seq=77 WHERE name='collections'")
        db.commit()
    return files_essential_source


def danbooru_essential_importer(callback=None):
    from modules.danbooru.user_schema import import_catalog_records
    return migrations.CatalogImporter('danbooru.essential-records', 1, callback or import_catalog_records)


def danbooru_essential_item(db, kind, values):
    return migrations.item_catalog.item_for_legacy(db, namespace='danbooru.' + kind,
        legacy_key=json.dumps(values, ensure_ascii=False, separators=(',', ':')))


@pytest.mark.parametrize('factory', [sqlite3.Row, 'dict'])
def test_danbooru_essential_candidate_preserves_all_owner_records_and_relationships(danbooru_essential_source, tmp_path, factory):
    from database_connections import _row_factory
    from modules.danbooru.artist_profiles import list_artist_profile_assets_from_conn
    source = danbooru_essential_source
    original = source.read_bytes()
    with closing(sqlite3.connect(source)) as db:
        before = publication_state(db)
        objects = {(row[0],row[1]) for row in db.execute('SELECT type,name FROM sqlite_master WHERE sql IS NOT NULL')}
    prepared = migrations.prepare_essential_candidate(source, tmp_path / 'danbooru', catalog_importers=[danbooru_essential_importer()])
    assert prepared.imported_items == {'danbooru.essential-records':31}
    with closing(sqlite3.connect(prepared.candidate)) as db:
        assert migrations._state(db, schema_objects=objects, table_names=set(before['table_rows'])) == before
        db.row_factory = _row_factory if factory == 'dict' else sqlite3.Row
        first = danbooru_essential_item(db, 'collection', [1])
        second = danbooru_essential_item(db, 'collection', [2])
        assert first['id'] != second['id']
        membership = danbooru_essential_item(db, 'collection-item', [1,11])
        relation = db.execute('SELECT target_item_id,kind FROM kv_item_relations WHERE source_item_id=?', (membership['id'],)).fetchone()
        assert relation['target_item_id'] == first['id'] and relation['kind'] == 'in-collection'
        followed = danbooru_essential_item(db, 'artist-post', ['author_a',333])
        author = danbooru_essential_item(db, 'artist-follow', ['author_a'])
        assert db.execute('SELECT target_item_id FROM kv_item_relations WHERE source_item_id=?', (followed['id'],)).fetchone()['target_item_id'] == author['id']
        assert followed['id'] != danbooru_essential_item(db, 'artist-post', ['author_b',333])['id']
        assert danbooru_essential_item(db, 'favorite-tag', ['same_tag','character']) != danbooru_essential_item(db, 'favorite-tag', ['same_tag','general'])
        assert db.execute('SELECT count(*) AS n FROM kv_items').fetchone()['n'] == 31
        assert db.execute('SELECT count(*) AS n FROM kv_item_relations').fetchone()['n'] == 5
        assets = db.execute('SELECT id,media_type,byte_size,hash_algorithm,content_hash FROM kv_assets').fetchall()
        assert len(assets) == 3 and len({row['id'] for row in assets}) == 3
        assert len({row['content_hash'] for row in assets}) == 2
        assert all(row['media_type'] == 'application/octet-stream' and row['byte_size'] is None and row['hash_algorithm'] == 'sha256' for row in assets)
        art = list_artist_profile_assets_from_conn(db, 'author_a')
        assert len(art) == 2 and {value.file_url for value in art} == {'/api/artist-profile-asset-files/1','/api/artist-profile-asset-files/2'}
        assert db.execute("SELECT body FROM tag_wiki_cache WHERE tag_name='saved_wiki'").fetchone()['body'] == '韓国語'*100000
        assert db.execute("SELECT seq FROM sqlite_sequence WHERE name='collections'").fetchone()['seq'] == 77
    assert migrations.validate_essential_candidate(source, prepared.attempt_directory, catalog_importers=[danbooru_essential_importer()]) == prepared
    assert source.read_bytes() == original


def test_danbooru_essential_preserves_path_hash_id_precedence_and_conflicting_references(danbooru_essential_source, tmp_path):
    prepared = migrations.prepare_essential_candidate(danbooru_essential_source, tmp_path / 'references', catalog_importers=[danbooru_essential_importer()])
    with closing(sqlite3.connect(prepared.candidate)) as db:
        references = {}
        for value in (11,12,13,14):
            item = danbooru_essential_item(db, 'favorite', [value])
            key = db.execute("SELECT capture_key FROM kv_item_captures WHERE item_id=? AND namespace='danbooru.file-reference'", (item['id'],)).fetchone()[0]
            references[value] = migrations.item_catalog.read_capture(db,item_id=item['id'],namespace='danbooru.file-reference',capture_key=key)['payload']
        assert [references[value]['match'] for value in (11,12,13,14)] == ['path','path','md5','file-id']
        assert references[11]['values']['local_md5'] == references[12]['values']['local_md5']
        assert danbooru_essential_item(db,'favorite',[11]) != danbooru_essential_item(db,'favorite',[12])
        member = danbooru_essential_item(db,'collection-item',[1,11])
        row = db.execute("SELECT payload_json FROM kv_item_captures WHERE item_id=? AND namespace='danbooru.file-reference'", (member['id'],)).fetchone()
        assert json.loads(row[0])['values']['file_path'] == '/old/conflicting.png'
        assert references[11]['values']['file_path'] == '/old/a.png'


def test_danbooru_essential_actual_favorite_collection_tag_and_art_readers_match_legacy(danbooru_essential_source, tmp_path, monkeypatch):
    from contextlib import contextmanager
    from database_connections import _row_factory
    from modules.danbooru.routers import collections, user_library
    from modules.danbooru.models import CollectionUpdate
    index_path = tmp_path / 'index.sqlite'
    with closing(sqlite3.connect(index_path)) as db:
        db.executescript('CREATE TABLE files(id INTEGER PRIMARY KEY,path TEXT,local_md5 TEXT,name TEXT,ext TEXT);'
                         'CREATE TABLE posts(file_id INTEGER PRIMARY KEY,width INTEGER,height INTEGER);')
        db.executemany('INSERT INTO files VALUES(?,?,?,?,?)', [(101,'/old/a.png','a'*32,'a.png','png'),
            (102,'/old/b.png','a'*32,'b.png','png'),(103,'/old/c.png','b'*32,'c.png','png'),(14,'/old/d.png','c'*32,'d.png','png')])
        db.commit()
    @contextmanager
    def connect(path):
        with closing(sqlite3.connect(path)) as db:
            db.row_factory = _row_factory
            yield db
    current = [danbooru_essential_source]
    monkeypatch.setattr(collections,'DATA_DB_PATH',index_path)
    monkeypatch.setattr(collections,'get_user_db',lambda:connect(current[0]))
    monkeypatch.setattr(user_library,'get_user_db',lambda:connect(current[0]))
    monkeypatch.setattr(user_library,'get_data_db',lambda:connect(index_path))
    def read():
        monkeypatch.setattr(user_library,'USER_DB_PATH',current[0])
        return dict(favorites=sorted(user_library.favorite_ids()),
            collections=[value.model_dump() for value in collections.list_collections()],
            tags=user_library.favorite_tag_names(),combos=[value.model_dump() for value in user_library.list_favorite_tag_combos()],
            blacklist=user_library.blacklist_tag_names())
    before_bytes = danbooru_essential_source.read_bytes()
    expected = read()
    assert expected['favorites'] == [14,101,102,103]
    prepared = migrations.prepare_essential_candidate(danbooru_essential_source, tmp_path / 'readers', catalog_importers=[danbooru_essential_importer()])
    current[0] = prepared.candidate
    assert read() == expected
    updated = collections.update_collection(1, CollectionUpdate(name='Renamed', description='Edited description'))
    assert updated.id == 1 and updated.name == 'Renamed'
    assert user_library.add_blacklist_tag('new_tag')
    assert 'new_tag' in user_library.blacklist_tag_names()
    with connect(prepared.candidate) as db:
        assert danbooru_essential_item(db,'collection',[1])['owner'] == 'danbooru'
    assert danbooru_essential_source.read_bytes() == before_bytes


def test_danbooru_essential_fresh_attempts_keep_ids_across_title_and_registered_root_relocation(danbooru_essential_source, tmp_path):
    source = danbooru_essential_source
    first = migrations.prepare_essential_candidate(source,tmp_path/'first',catalog_importers=[danbooru_essential_importer()])
    with closing(sqlite3.connect(first.candidate)) as db:
        before = files_essential_identities(db)
        old_root = danbooru_essential_item(db,'registered-folder',['old-folder'])
    with closing(sqlite3.connect(source)) as db:
        db.execute("UPDATE collections SET name='Renamed',description='New body' WHERE id=1")
        db.execute("UPDATE favorites SET file_path='/relocated/a.png' WHERE file_id=11")
        db.execute("UPDATE registered_folders SET name='new-folder',path='/relocated/root',display_name='Renamed' WHERE root_id='root-fixed'")
        db.execute("UPDATE artist_profile_assets SET file_path='/relocated/art.png' WHERE id=1")
        db.commit()
    second = migrations.prepare_essential_candidate(source,tmp_path/'second',catalog_importers=[danbooru_essential_importer()])
    with closing(sqlite3.connect(second.candidate)) as db:
        after = files_essential_identities(db)
        assert after['kv_items'] == before['kv_items'] and after['kv_assets'] == before['kv_assets']
        assert after['kv_item_assets'] == before['kv_item_assets']
        assert danbooru_essential_item(db,'registered-folder',['new-folder']) == old_root
        assert migrations.item_catalog.item_for_legacy(db,namespace='danbooru.root',legacy_key='root-fixed') == old_root
        assert danbooru_essential_item(db,'registered-folder',['other-folder']) != old_root
    with pytest.raises(migrations.PreparationError,match='Source changed'):
        migrations.validate_essential_candidate(source,first.attempt_directory,catalog_importers=[danbooru_essential_importer()])


@pytest.mark.parametrize('factory',[None,sqlite3.Row,'dict'])
def test_danbooru_essential_direct_import_is_exact_and_retains_caller_rollback(danbooru_essential_source,factory):
    from database_connections import _row_factory
    from modules.danbooru.user_schema import import_catalog_records
    with closing(files_essential_connection(danbooru_essential_source)) as db:
        db.row_factory = _row_factory if factory == 'dict' else factory
        before = publication_state(db)
        db.execute('BEGIN')
        assert import_catalog_records(db) == 31 and db.in_transaction
        captured = publication_state(db)
        assert import_catalog_records(db) == 31 and publication_state(db) == captured
        assert db.row_factory == (_row_factory if factory == 'dict' else factory)
        db.rollback()
        assert publication_state(db) == before
        with pytest.raises(ValueError,match='caller transaction'): import_catalog_records(db)


def test_danbooru_essential_mutable_reference_observations_append_without_replacing_identity(danbooru_essential_source):
    from modules.danbooru.user_schema import import_catalog_records
    with closing(files_essential_connection(danbooru_essential_source)) as db:
        db.execute('BEGIN'); import_catalog_records(db); db.commit()
        item = danbooru_essential_item(db,'favorite',[11])
        before = db.execute("SELECT capture_key,payload_json FROM kv_item_captures WHERE item_id=?",(item['id'],)).fetchall()
        db.execute("UPDATE favorites SET file_path='/new/a.png' WHERE file_id=11"); db.commit()
        db.execute('BEGIN'); import_catalog_records(db); db.commit()
        assert danbooru_essential_item(db,'favorite',[11]) == item
        after = db.execute('SELECT capture_key,payload_json FROM kv_item_captures WHERE item_id=?',(item['id'],)).fetchall()
        assert len(after) == len(before)+1 and set(before) <= set(after)


@pytest.mark.parametrize('failure',['late-alias','late-capture','late-asset','late-relation'])
def test_danbooru_essential_late_failure_rolls_back_all_owner_additions(danbooru_essential_source,monkeypatch,failure):
    from modules.danbooru.user_schema import import_catalog_records
    service = migrations.item_catalog
    with closing(files_essential_connection(danbooru_essential_source)) as db:
        if failure == 'late-alias': service.ensure_item(db,namespace='danbooru.registered-folder',legacy_key='["old-folder"]',owner='foreign',item_type='other')
        before = files_essential_identities(db)
        target = {'late-capture':'record_capture','late-asset':'record_asset','late-relation':'relate_items'}.get(failure)
        if target:
            original = getattr(service,target)
            calls = 0
            def fail(*a,**kw):
                nonlocal calls
                value = original(*a,**kw); calls += 1
                if calls == {'late-capture':44,'late-asset':3,'late-relation':5}[failure]: raise InterruptedError('Interrupted owner import')
                return value
            monkeypatch.setattr(service,target,fail)
        db.execute('BEGIN')
        db.execute("INSERT INTO user_settings(key,value) VALUES('pending','Keep caller write')")
        with pytest.raises((ValueError,InterruptedError)): import_catalog_records(db)
        assert db.in_transaction and files_essential_identities(db) == before
        assert db.execute("SELECT value FROM user_settings WHERE key='pending'").fetchone()[0] == 'Keep caller write'


@pytest.mark.parametrize('table,column,value', [('favorites','file_id',0),('favorite_tags','tag_name','bad\0tag'),
    ('collections','id',0),('user_image_tags','id',0),('registered_folders','root_id','bad\0root'),
    ('artist_profile_assets','content_hash','bad hash'),('favorites','file_path','bad\0path'),
    ('tag_removals','local_md5',b'not text')])
def test_danbooru_essential_bad_identity_or_asset_refuses_without_source_changes(danbooru_essential_source,tmp_path,table,column,value):
    with closing(sqlite3.connect(danbooru_essential_source)) as db:
        db.execute('UPDATE '+table+' SET '+column+'=? WHERE rowid=(SELECT min(rowid) FROM '+table+')',(value,)); db.commit()
    original = danbooru_essential_source.read_bytes()
    with pytest.raises(migrations.PreparationError):
        migrations.prepare_essential_candidate(danbooru_essential_source,tmp_path/'bad',catalog_importers=[danbooru_essential_importer()])
    assert danbooru_essential_source.read_bytes() == original
    with closing(sqlite3.connect(tmp_path/'bad/essential.sqlite')) as db:
        assert db.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 0


@pytest.mark.parametrize('table,column', [('collection_items','collection_id'),('artist_follow_posts','tag_name')])
def test_danbooru_essential_orphan_parents_refuse_without_partial_import(danbooru_essential_source,table,column):
    from modules.danbooru.user_schema import import_catalog_records
    with closing(files_essential_connection(danbooru_essential_source)) as db:
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('UPDATE '+table+' SET '+column+'=? WHERE rowid=(SELECT min(rowid) FROM '+table+')',
                   (999 if column=='collection_id' else 'missing_artist',)); db.commit()
        before = publication_state(db); db.execute('BEGIN')
        with pytest.raises(ValueError,match='unknown parent'): import_catalog_records(db)
        assert publication_state(db) == before


@pytest.mark.parametrize('schema,count', [('',0),
    ('CREATE TABLE favorites(file_id INTEGER PRIMARY KEY); INSERT INTO favorites VALUES(7);',1),
    ("CREATE TABLE registered_folders(name TEXT PRIMARY KEY); INSERT INTO registered_folders VALUES('old');",1),
    ('CREATE TABLE collections(id INTEGER PRIMARY KEY); CREATE TABLE collection_items(collection_id INTEGER,file_id INTEGER);',0)])
def test_danbooru_essential_absent_and_old_minimal_owner_schemas(schema,count):
    from modules.danbooru.user_schema import import_catalog_records
    with closing(sqlite3.connect(':memory:')) as db:
        db.executescript(schema); migrations.item_catalog.ensure_catalog_schema(db); db.execute('BEGIN')
        assert import_catalog_records(db) == count


@pytest.mark.parametrize('schema', ['CREATE TABLE favorites(other INTEGER);',
    'CREATE TABLE favorites(file_id INTEGER); INSERT INTO favorites VALUES(1),(1);',
    'CREATE TABLE artist_profile_assets(id INTEGER PRIMARY KEY);',
    'CREATE TABLE collection_items(collection_id INTEGER,file_id INTEGER); INSERT INTO collection_items VALUES(1,2);',
    "CREATE TABLE artist_follow_posts(tag_name TEXT,danbooru_post_id INTEGER); INSERT INTO artist_follow_posts VALUES('absent',3);"])
def test_danbooru_essential_unknown_duplicate_or_missing_parent_schema_refuses(schema):
    from modules.danbooru.user_schema import import_catalog_records
    with closing(sqlite3.connect(':memory:')) as db:
        db.executescript(schema); migrations.item_catalog.ensure_catalog_schema(db); db.execute('BEGIN')
        with pytest.raises(ValueError): import_catalog_records(db)
        assert db.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 0


def test_danbooru_essential_import_never_resolves_media_opens_index_or_starts_workers(danbooru_essential_source,monkeypatch):
    from modules.danbooru import user_schema
    import database, socket
    with closing(files_essential_connection(danbooru_essential_source)) as db:
        def blocked(*a,**kw): raise AssertionError('No media, index, runtime or network access')
        with monkeypatch.context() as scoped:
            for name in ('open','stat','resolve'): scoped.setattr(Path,name,blocked)
            scoped.setattr(database,'get_data_db',blocked); scoped.setattr(database,'get_user_db',blocked)
            scoped.setattr(socket,'create_connection',blocked); scoped.setattr(user_schema,'migrate_user_tables',blocked)
            db.execute('BEGIN'); assert user_schema.import_catalog_records(db) == 31; db.commit()


def test_danbooru_essential_combined_files_preparation_and_snapshot_restore(danbooru_essential_source,tmp_path):
    importers = [files_essential_importer(),danbooru_essential_importer()]
    prepared = migrations.prepare_essential_candidate(danbooru_essential_source,tmp_path/'both',catalog_importers=importers)
    assert prepared.imported_items == {'danbooru.essential-records':31,'files.annotations':5}
    restored = tmp_path/'restored.sqlite'
    migrations.snapshot_user_database(prepared.candidate,restored)
    with closing(sqlite3.connect(prepared.candidate)) as before, closing(sqlite3.connect(restored)) as after:
        assert publication_state(before) == publication_state(after)
        assert before.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 36
        assert before.execute('SELECT count(*) FROM kv_assets').fetchone()[0] == 6
        assert before.execute('SELECT count(*) FROM favorites').fetchone()[0] == 4
        assert before.execute('SELECT count(*) FROM files_annotations').fetchone()[0] == 5
    assert migrations.prepare_essential_candidate(danbooru_essential_source,prepared.attempt_directory,catalog_importers=importers) == prepared


@pytest.mark.parametrize('change',['hash','artist'])
def test_danbooru_essential_archived_capture_identity_and_bytes_cannot_be_replaced(danbooru_essential_source,change):
    from modules.danbooru.user_schema import import_catalog_records
    with closing(files_essential_connection(danbooru_essential_source)) as db:
        db.execute('BEGIN'); import_catalog_records(db); db.commit()
        if change=='hash': db.execute("UPDATE artist_profile_assets SET content_hash=? WHERE id=2",('f'*64,))
        else: db.execute("UPDATE artist_profile_assets SET tag_name='different_artist' WHERE id=2")
        db.commit(); before = publication_state(db); db.execute('BEGIN')
        with pytest.raises(migrations.item_catalog.CatalogConflict): import_catalog_records(db)
        assert publication_state(db) == before


@pytest.fixture
def manga_essential_source(files_essential_source):
    from types import SimpleNamespace
    from uuid import uuid4
    from modules.manga import user_schema, progress
    from services.library_categories import CategoryStore
    store = CategoryStore('manga')
    with closing(sqlite3.connect(files_essential_source)) as db:
        db.row_factory = sqlite3.Row
        user_schema.publish(db)
        db.execute("UPDATE files_sources SET role='manga'"); db.commit()
        first = store.create(db,'Favorites',str(uuid4()))['id']
        second = store.create(db,'Finished',str(uuid4()))['id']
        validate = lambda *a: None
        for source,path,selected,expected in [('src-first','Story',[first,second],0),
            ('src-first','Story',[],1),('src-first','Story',[first],2),
            ('src-hidden','Story',[second],0),('src-first','',[first],0)]:
            store.set_membership(db,source,path,selected,expected,str(uuid4()),validate)
        progress.begin(db,'src-first','Story/01','folder',0)
        progress.save(db,SimpleNamespace(source_id='src-first',series_path='Previous context',path='Story/01',kind='folder',
            revision='a'*64,generation=1,sequence=2,page=1,mode='paged',anchor=0.5),8)
        progress.begin(db,'src-first','Story/01','folder',1)
        progress.save(db,SimpleNamespace(source_id='src-first',series_path='Story',path='Story/01',kind='folder',
            revision='b'*64,generation=2,sequence=5,page=2,mode='vertical',anchor=0.33),10)
        for source,path,kind,generation in [('src-first','Story/02.cbz','archive',1),
            ('src-hidden','Story/01','folder',3),('forgotten-source','Unknown folder/chapter','folder',0),
            ('src-first','','folder',1)]:
            db.execute('INSERT INTO manga_reading_sessions VALUES(?,?,?,?)',(source,path,kind,generation))
        for source,series,path,kind,revision,generation in [('src-first','Story','Story/01','archive','c'*64,1),
            ('src-first','Story','Story/02.cbz','archive','d'*64,1),
            ('src-hidden','Story','Story/01','folder','a'*64,3),('src-first','','','folder','e'*64,1)]:
            db.execute('INSERT INTO manga_reading_progress(source_id,series_path,path,kind,revision,page_count,page,mode,anchor,generation,sequence) '
                       'VALUES(?,?,?,?,?,3,1,\'paged\',0.5,?,1)',(source,series,path,kind,revision,generation))
        historical = json.dumps(['removed-category',second])
        db.execute('INSERT INTO manga_category_updates(request_id,source_id,path,expected_version,version,category_ids) '
                   'VALUES(?,?,?,0,1,?)',(str(uuid4()),'forgotten-source','Lost',historical))
        db.execute('ALTER TABLE manga_reading_progress ADD COLUMN future_blob BLOB')
        db.execute('UPDATE manga_reading_progress SET future_blob=?',(b'\0\xffpreserve',))
        db.commit()
    return files_essential_source


def manga_essential_importer(callback=None):
    from modules.manga.user_schema import import_catalog_records
    return migrations.CatalogImporter('manga.essential-records',1,callback or import_catalog_records)


def manga_essential_item(db,kind,key):
    cursor = db.cursor(); cursor.row_factory = sqlite3.Row
    try:
        for row in cursor.execute("SELECT i.id,i.owner,i.item_type FROM kv_items i JOIN kv_item_captures c ON c.item_id=i.id "
                "WHERE i.owner='manga' AND i.item_type=? AND c.namespace='manga.record' AND c.capture_key='identity'",(kind,)):
            captured = migrations.item_catalog.read_capture(db,item_id=row['id'],namespace='manga.record',capture_key='identity')
            if captured['payload'] == dict(kind=kind,key=key): return dict(row)
        return None
    finally:
        cursor.close()


@pytest.mark.parametrize('factory',[sqlite3.Row,'dict'])
def test_manga_essential_candidate_preserves_actual_reading_category_state_and_ids(manga_essential_source,tmp_path,factory):
    from database_connections import _row_factory
    from modules.manga import progress
    from services.library_categories import CategoryStore
    source = manga_essential_source
    original = source.read_bytes()
    with closing(sqlite3.connect(source)) as db:
        before = publication_state(db)
        objects = {(row[0],row[1]) for row in db.execute('SELECT type,name FROM sqlite_master WHERE sql IS NOT NULL')}
    prepared = migrations.prepare_essential_candidate(source,tmp_path/'manga',catalog_importers=[manga_essential_importer()])
    assert prepared.imported_items == {'manga.essential-records':26}
    with closing(sqlite3.connect(prepared.candidate)) as db:
        assert migrations._state(db,schema_objects=objects,table_names=set(before['table_rows'])) == before
        db.row_factory = _row_factory if factory == 'dict' else sqlite3.Row
        saved = progress.read(db,'src-first','Story/01','folder','b'*64)
        assert saved['generation'] == 2 and saved['position']['sequence'] == 5
        assert saved['position']['page'] == 2 and saved['position']['mode'] == 'vertical' and saved['position']['anchor'] == 0.33
        old = progress.read(db,'src-first','Story/01','folder','a'*64)
        assert old['position']['generation'] == 1 and old['position']['series_path'] == 'Previous context'
        store = CategoryStore('manga')
        categories = store.listing(db,0,120)['items']
        for category in categories:
            assert manga_essential_item(db,'category',[category['id']])['id'] == category['id']
        assert store.membership(db,'src-first','Story')['version'] == 3
        assert len(store.membership(db,'src-first','Story')['category_ids']) == 1
        assert db.execute('SELECT count(*) AS n FROM manga_category_memberships WHERE active=0').fetchone()['n'] == 1
        assert db.execute('SELECT count(*) AS n FROM manga_reading_progress').fetchone()['n'] == 6
        assert db.execute('SELECT count(*) AS n FROM kv_items').fetchone()['n'] == 37
        assert db.execute('SELECT count(*) AS n FROM kv_item_relations').fetchone()['n'] == 40
        assert db.execute('SELECT count(*) AS n FROM kv_assets').fetchone()['n'] == 0
        assert db.execute('SELECT future_blob FROM manga_reading_progress LIMIT 1').fetchone()['future_blob'] == b'\0\xffpreserve'
    assert migrations.validate_essential_candidate(source,prepared.attempt_directory,catalog_importers=[manga_essential_importer()]) == prepared
    assert source.read_bytes() == original


def test_manga_essential_distinguishes_sources_kinds_revisions_and_root_chapters(manga_essential_source,tmp_path):
    prepared = migrations.prepare_essential_candidate(manga_essential_source,tmp_path/'identities',catalog_importers=[manga_essential_importer()])
    with closing(sqlite3.connect(prepared.candidate)) as db:
        chapter = manga_essential_item(db,'chapter',['src-first','Story/01','folder'])
        assert chapter != manga_essential_item(db,'chapter',['src-first','Story/01','archive'])
        assert chapter != manga_essential_item(db,'chapter',['src-hidden','Story/01','folder'])
        assert manga_essential_item(db,'chapter',['src-first','','folder'])
        assert manga_essential_item(db,'series',['src-first',''])
        first = manga_essential_item(db,'reading-progress',['src-first','Story/01','folder','a'*64])
        second = manga_essential_item(db,'reading-progress',['src-first','Story/01','folder','b'*64])
        assert first != second
        for saved,series in [(first,'Previous context'),(second,'Story')]:
            parent = manga_essential_item(db,'series',['src-first',series])
            assert db.execute("SELECT target_item_id FROM kv_item_relations WHERE source_item_id=? AND kind='chapter'",(saved['id'],)).fetchone()[0] == chapter['id']
            assert db.execute("SELECT target_item_id FROM kv_item_relations WHERE source_item_id=? AND kind='series'",(saved['id'],)).fetchone()[0] == parent['id']
        session = manga_essential_item(db,'reading-session',['forgotten-source','Unknown folder/chapter','folder'])
        assert session and manga_essential_item(db,'chapter',['forgotten-source','Unknown folder/chapter','folder'])
        assert manga_essential_item(db,'series',['forgotten-source','Unknown folder']) is None
        assert db.execute('SELECT kind FROM kv_item_relations WHERE source_item_id=?',(session['id'],)).fetchall() == [('chapter',)]


def test_manga_essential_receipts_preserve_inactive_and_unresolved_history_without_phantom_categories(manga_essential_source,tmp_path):
    prepared = migrations.prepare_essential_candidate(manga_essential_source,tmp_path/'history',catalog_importers=[manga_essential_importer()])
    with closing(sqlite3.connect(prepared.candidate)) as db:
        db.row_factory = sqlite3.Row
        receipt = db.execute("SELECT * FROM manga_category_updates WHERE source_id='forgotten-source'").fetchone()
        saved = manga_essential_item(db,'category-update',[receipt['request_id']])
        captured = migrations.item_catalog.read_capture(db,item_id=saved['id'],namespace='manga.category-receipt',capture_key=receipt['request_id'])
        assert captured['payload']['category_ids_json'] == receipt['category_ids']
        assert json.loads(receipt['category_ids'])[0] == 'removed-category'
        assert manga_essential_item(db,'category',['removed-category']) is None
        selected = db.execute("SELECT target_item_id,position FROM kv_item_relations WHERE source_item_id=? AND kind='selected-category'",(saved['id'],)).fetchall()
        assert len(selected) == 1 and selected[0]['position'] == 1
        assert selected[0]['target_item_id'] == json.loads(receipt['category_ids'])[1]
        inactive = db.execute('SELECT category_id,source_id,path FROM manga_category_memberships WHERE active=0').fetchone()
        membership = manga_essential_item(db,'category-membership',[inactive['category_id'],inactive['source_id'],inactive['path']])
        assert membership and db.execute('SELECT count(*) FROM kv_item_relations WHERE source_item_id=?',(membership['id'],)).fetchone()[0] == 2


def test_manga_essential_existing_progress_saves_keep_generation_sequence_and_revision_rules(manga_essential_source,tmp_path):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from modules.manga import progress
    prepared = migrations.prepare_essential_candidate(manga_essential_source,tmp_path/'progress',catalog_importers=[manga_essential_importer()])
    original = manga_essential_source.read_bytes()
    with closing(sqlite3.connect(prepared.candidate)) as db:
        db.row_factory = sqlite3.Row
        assert progress.begin(db,'src-first','Story/01','folder',2) == {'generation':3}
        payload = SimpleNamespace(source_id='src-first',series_path='Story',path='Story/01',kind='folder',
            revision='b'*64,generation=3,sequence=1,page=3,mode='vertical',anchor=0.2)
        saved = progress.save(db,payload,10)
        assert progress.save(db,payload,10) == saved
        db.rollback()  # The unchanged retry closes its request connection normally.
        payload.page = 4
        with pytest.raises(HTTPException) as stale: progress.save(db,payload,10)
        assert stale.value.status_code == 409
        db.rollback()
        payload.sequence = 2
        assert progress.save(db,payload,10)['page'] == 4
        payload.generation = 2
        with pytest.raises(HTTPException) as stale: progress.save(db,payload,10)
        assert stale.value.status_code == 409
        db.rollback()
        assert progress.read(db,'src-first','Story/01','folder','a'*64)['position']['series_path'] == 'Previous context'
        assert manga_essential_item(db,'reading-progress',['src-first','Story/01','folder','b'*64])
    assert manga_essential_source.read_bytes() == original


def test_manga_essential_existing_category_retry_filter_and_edit_behavior_matches_legacy(manga_essential_source,tmp_path):
    from uuid import uuid4
    from fastapi import HTTPException
    from services.library_categories import CategoryStore
    store = CategoryStore('manga')
    prepared = migrations.prepare_essential_candidate(manga_essential_source,tmp_path/'categories',catalog_importers=[manga_essential_importer()])
    with closing(sqlite3.connect(manga_essential_source)) as source, closing(sqlite3.connect(prepared.candidate)) as db:
        source.row_factory = db.row_factory = sqlite3.Row
        assert store.listing(source,0,120) == store.listing(db,0,120)
        before = store.membership(db,'src-first','Story')
        receipt = db.execute("SELECT * FROM manga_category_updates WHERE source_id='src-first' AND path='Story' AND version=3").fetchone()
        validate = lambda *a: None
        assert store.set_membership(db,'src-first','Story',before['category_ids'],2,receipt['request_id'],validate) == before
        request = str(uuid4())
        assert store.set_membership(db,'src-first','Story',[],3,request,validate) == {'version':4,'category_ids':[]}
        assert store.set_membership(db,'src-first','Story',[],3,request,validate)['version'] == 4
        with pytest.raises(HTTPException) as stale: store.set_membership(db,'src-first','Story',before['category_ids'],3,str(uuid4()),validate)
        assert stale.value.status_code == 409
        items = [dict(source_id='src-first',path='Story'),dict(source_id='src-hidden',path='Story')]
        assert store.filter_items(db,items,'default') == [items[0]]
        assert store.membership(source,'src-first','Story') == before
        assert db.execute('SELECT count(*) FROM manga_reading_progress').fetchone()[0] == 6


def test_manga_essential_root_relocation_and_category_title_change_keep_all_ids(manga_essential_source,tmp_path):
    source = manga_essential_source
    first = migrations.prepare_essential_candidate(source,tmp_path/'first',catalog_importers=[manga_essential_importer()])
    with closing(sqlite3.connect(first.candidate)) as db: expected = files_essential_identities(db)
    with closing(sqlite3.connect(source)) as db:
        db.execute("UPDATE files_sources SET path='/relocated/library',display_name='Renamed' WHERE source_id='src-first'")
        db.execute("UPDATE manga_categories SET name='New display name',name_key='new display name' WHERE rowid=(SELECT min(rowid) FROM manga_categories)")
        db.commit()
    second = migrations.prepare_essential_candidate(source,tmp_path/'second',catalog_importers=[manga_essential_importer()])
    with closing(sqlite3.connect(second.candidate)) as db: assert files_essential_identities(db) == expected
    with pytest.raises(migrations.PreparationError,match='Source changed'):
        migrations.validate_essential_candidate(source,first.attempt_directory,catalog_importers=[manga_essential_importer()])


@pytest.mark.parametrize('factory',[None,sqlite3.Row,'dict'])
def test_manga_essential_direct_import_exact_retry_and_caller_rollback(manga_essential_source,factory):
    from database_connections import _row_factory
    from modules.manga.user_schema import import_catalog_records
    with closing(files_essential_connection(manga_essential_source)) as db:
        db.row_factory = _row_factory if factory == 'dict' else factory
        before = publication_state(db); db.execute('BEGIN')
        assert import_catalog_records(db) == 26 and db.in_transaction
        captured = publication_state(db)
        assert import_catalog_records(db) == 26 and publication_state(db) == captured
        assert db.row_factory == (_row_factory if factory == 'dict' else factory)
        db.rollback(); assert publication_state(db) == before
        with pytest.raises(ValueError,match='caller transaction'): import_catalog_records(db)


@pytest.mark.parametrize('failure',['alias','receipt','relation','historical-category'])
def test_manga_essential_late_failure_keeps_original_catalog_and_pending_caller_writes(manga_essential_source,monkeypatch,failure):
    from modules.manga.user_schema import import_catalog_records
    service = migrations.item_catalog
    with closing(files_essential_connection(manga_essential_source)) as db:
        if failure in ('alias','historical-category'):
            category = db.execute('SELECT max(id) FROM manga_categories').fetchone()[0] if failure=='alias' else 'removed-category'
            service.ensure_item(db,namespace='manga.category',legacy_key=category,owner='foreign',item_type='other')
        before = files_essential_identities(db)
        if failure in ('receipt','relation'):
            target = 'record_capture' if failure=='receipt' else 'relate_items'
            original = getattr(service,target)
            def fail(*a,**kw):
                value = original(*a,**kw)
                if kw.get('namespace')=='manga.category-receipt' or kw.get('kind')=='selected-category': raise InterruptedError('Late Manga import failure')
                return value
            monkeypatch.setattr(service,target,fail)
        db.execute('BEGIN'); db.execute("INSERT INTO user_settings(key,value) VALUES('pending','Keep caller write')")
        with pytest.raises((ValueError,InterruptedError)): import_catalog_records(db)
        assert db.in_transaction and files_essential_identities(db) == before
        assert db.execute("SELECT value FROM user_settings WHERE key='pending'").fetchone()[0] == 'Keep caller write'


@pytest.mark.parametrize('table,field,value', [('manga_reading_sessions','kind','other'),
    ('manga_reading_progress','revision',''),('manga_reading_progress','series_path','bad\0path'),
    ('manga_reading_progress','source_id',''),('manga_category_memberships','path',b'not text'),
    ('manga_categories','id',''),('manga_category_updates','request_id','')])
def test_manga_essential_bad_keys_refuse_without_source_changes(manga_essential_source,tmp_path,table,field,value):
    with closing(sqlite3.connect(manga_essential_source)) as db:
        db.execute('UPDATE '+table+' SET '+field+'=? WHERE rowid=(SELECT min(rowid) FROM '+table+')',(value,)); db.commit()
    original = manga_essential_source.read_bytes()
    with pytest.raises(migrations.PreparationError):
        migrations.prepare_essential_candidate(manga_essential_source,tmp_path/'bad',catalog_importers=[manga_essential_importer()])
    assert manga_essential_source.read_bytes() == original
    with closing(sqlite3.connect(tmp_path/'bad/essential.sqlite')) as db: assert db.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 0


@pytest.mark.parametrize('raw',['{bad','{}','[null]','[""]',' '*65537],
    ids=['invalid-json','object','null-entry','empty-id','oversized'])
def test_manga_essential_malformed_receipts_roll_back_entire_batch(manga_essential_source,raw):
    from modules.manga.user_schema import import_catalog_records
    with closing(files_essential_connection(manga_essential_source)) as db:
        db.execute('UPDATE manga_category_updates SET category_ids=? WHERE rowid=(SELECT max(rowid) FROM manga_category_updates)',(raw,)); db.commit()
        before = publication_state(db); db.execute('BEGIN')
        with pytest.raises(ValueError): import_catalog_records(db)
        assert publication_state(db) == before


@pytest.mark.parametrize('schema,count', [('',0),
    ("CREATE TABLE manga_categories(id TEXT PRIMARY KEY); INSERT INTO manga_categories VALUES('old-category');",1),
    ("CREATE TABLE manga_reading_sessions(source_id TEXT,path TEXT,kind TEXT); INSERT INTO manga_reading_sessions VALUES('missing','','folder');",1),
    ("CREATE TABLE manga_reading_progress(source_id TEXT,path TEXT,kind TEXT,revision TEXT,series_path TEXT); INSERT INTO manga_reading_progress VALUES('missing','chapter','folder','old-revision','series');",1),
    ("CREATE TABLE manga_categories(id TEXT PRIMARY KEY); INSERT INTO manga_categories VALUES('old-category'); CREATE TABLE manga_category_memberships(category_id TEXT,source_id TEXT,path TEXT); INSERT INTO manga_category_memberships VALUES('old-category','missing','series');",2)])
def test_manga_essential_absent_and_old_minimal_owner_schemas(schema,count):
    from modules.manga.user_schema import import_catalog_records
    with closing(sqlite3.connect(':memory:')) as db:
        db.executescript(schema); migrations.item_catalog.ensure_catalog_schema(db); db.execute('BEGIN')
        assert import_catalog_records(db) == count


@pytest.mark.parametrize('schema', ['CREATE TABLE manga_categories(other TEXT);',
    "CREATE TABLE manga_reading_sessions(source_id TEXT,path TEXT,kind TEXT); INSERT INTO manga_reading_sessions VALUES('a','b','folder'),('a','b','folder');",
    'CREATE TABLE manga_category_updates(request_id TEXT PRIMARY KEY);',
    "CREATE TABLE manga_category_memberships(category_id TEXT,source_id TEXT,path TEXT); INSERT INTO manga_category_memberships VALUES('absent','a','b');"])
def test_manga_essential_unknown_duplicate_or_orphan_schema_refuses(schema):
    from modules.manga.user_schema import import_catalog_records
    with closing(sqlite3.connect(':memory:')) as db:
        db.executescript(schema); migrations.item_catalog.ensure_catalog_schema(db); db.execute('BEGIN')
        with pytest.raises(ValueError): import_catalog_records(db)
        assert db.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 0


def test_manga_essential_valid_maximum_path_keys_are_bounded_without_losing_tuple():
    from modules.manga.user_schema import publish,import_catalog_records
    source,path = 's'*256,'p'*4096
    with closing(sqlite3.connect(':memory:')) as db:
        publish(db); db.execute('INSERT INTO manga_reading_sessions VALUES(?,?,?,0)',(source,path,'folder')); db.commit()
        migrations.item_catalog.ensure_catalog_schema(db); db.execute('BEGIN')
        assert import_catalog_records(db) == 1
        assert manga_essential_item(db,'chapter',[source,path,'folder'])
        assert max(row[0] for row in db.execute('SELECT length(legacy_key) FROM kv_legacy_keys')) < 4096


def test_manga_essential_combined_files_snapshot_restore_retains_reader_state(manga_essential_source,tmp_path):
    from modules.manga import progress
    importers = [files_essential_importer(),manga_essential_importer()]
    prepared = migrations.prepare_essential_candidate(manga_essential_source,tmp_path/'combined',catalog_importers=importers)
    assert prepared.imported_items == {'files.annotations':5,'manga.essential-records':26}
    restored = tmp_path/'restored.sqlite'; migrations.snapshot_user_database(prepared.candidate,restored)
    with closing(sqlite3.connect(prepared.candidate)) as old, closing(sqlite3.connect(restored)) as db:
        assert publication_state(db) == publication_state(old)
        db.row_factory = sqlite3.Row
        assert progress.read(db,'src-first','Story/01','folder','b'*64)['position']['sequence'] == 5
        assert db.execute('SELECT count(*) FROM manga_reading_progress').fetchone()[0] == 6
        assert db.execute('SELECT count(*) FROM files_annotations').fetchone()[0] == 5
    assert migrations.prepare_essential_candidate(manga_essential_source,prepared.attempt_directory,catalog_importers=importers) == prepared


def test_manga_essential_immutable_receipt_conflict_preserves_all_existing_facts(manga_essential_source):
    from modules.manga.user_schema import import_catalog_records
    with closing(files_essential_connection(manga_essential_source)) as db:
        db.execute('BEGIN'); import_catalog_records(db); db.commit()
        db.execute("UPDATE manga_category_updates SET category_ids='[]' WHERE source_id='forgotten-source'"); db.commit()
        before = publication_state(db); db.execute('BEGIN')
        with pytest.raises(migrations.item_catalog.CatalogConflict): import_catalog_records(db)
        assert publication_state(db) == before


def test_manga_essential_fresh_import_does_not_load_engine_jobs_or_runtime():
    code = r'''
import builtins,sqlite3,sys
sys.path.insert(0,sys.argv[1])
original=builtins.__import__
def guarded(name,globals=None,locals=None,fromlist=(),level=0):
    if level==0 and (name in ('config','database','lifecycle','suite_modules','modules.manga.library','modules.manga.router','modules.manga.page_cache') or name.startswith(('modules.youtube','modules.twitter','yt_dlp','PIL'))):
        raise AssertionError('Unexpected runtime/engine import: '+name)
    return original(name,globals,locals,fromlist,level)
builtins.__import__=guarded
from modules.manga.user_schema import import_catalog_records
from services.item_catalog import ensure_catalog_schema
with sqlite3.connect(':memory:') as db:
    db.executescript("CREATE TABLE manga_reading_sessions(source_id TEXT,path TEXT,kind TEXT); INSERT INTO manga_reading_sessions VALUES('missing','','folder');")
    ensure_catalog_schema(db); db.execute('BEGIN')
    assert import_catalog_records(db)==1
    db.commit()
assert not any(name in sys.modules for name in ('config','database','lifecycle','modules.manga.library','modules.manga.router','modules.manga.page_cache'))
'''
    options = dict(capture_output=True,timeout=30)
    if os.name=='nt': options['creationflags'] = subprocess.CREATE_NO_WINDOW
    completed = subprocess.run([sys.executable,'-c',code,str(Path(__file__).resolve().parents[1]/'backend')],**options)
    assert completed.returncode == 0, completed.stderr.decode('utf-8','replace')


@pytest.fixture
def video_essential_source(files_essential_source):
    from uuid import uuid4
    from modules.video import user_schema
    from services.library_categories import CategoryStore
    store = CategoryStore('video')
    with closing(sqlite3.connect(files_essential_source)) as db:
        db.row_factory = sqlite3.Row
        user_schema.publish(db)
        db.execute("UPDATE files_sources SET role='video'"); db.commit()
        first = store.create(db,'Favorites',str(uuid4()))['id']
        second = store.create(db,'Finished',str(uuid4()))['id']
        for source,path,selected,expected in [('src-first','Movie/01.mp4',[first,second],0),
            ('src-first','Movie/01.mp4',[],1),('src-first','Movie/01.mp4',[first],2),
            ('src-hidden','Movie/01.mp4',[second],0),('src-first','',[first],0)]:
            store.set_membership(db,source,path,selected,expected,str(uuid4()),lambda *a: None)
        db.execute('INSERT INTO video_category_updates(request_id,source_id,path,expected_version,version,category_ids) '
                   'VALUES(?,?,?,0,1,?)',(str(uuid4()),'forgotten-source','Lost.mp4',json.dumps(['removed-category',second])))
        db.execute('ALTER TABLE video_categories ADD COLUMN future_blob BLOB')
        db.execute('UPDATE video_categories SET future_blob=?',(b'\0\xffpreserve',)); db.commit()
    return files_essential_source


def video_essential_importer(callback=None):
    from modules.video.user_schema import import_catalog_records
    return migrations.CatalogImporter('video.essential-records',1,callback or import_catalog_records)


def video_essential_item(db,kind,key):
    cursor = db.cursor(); cursor.row_factory = sqlite3.Row
    try:
        for row in cursor.execute("SELECT i.id,i.owner,i.item_type FROM kv_items i JOIN kv_item_captures c ON c.item_id=i.id "
                "WHERE i.owner='video' AND i.item_type=? AND c.namespace='video.record' AND c.capture_key='identity'",(kind,)):
            captured = migrations.item_catalog.read_capture(db,item_id=row['id'],namespace='video.record',capture_key='identity')
            if captured['payload'] == dict(kind=kind,key=key): return dict(row)
        return None
    finally:
        cursor.close()


@pytest.mark.parametrize('factory',[sqlite3.Row,'dict'])
def test_video_essential_candidate_preserves_all_legacy_records_and_category_ids(video_essential_source,tmp_path,factory):
    from database_connections import _row_factory
    from services.library_categories import CategoryStore
    source = video_essential_source
    original = source.read_bytes()
    with closing(sqlite3.connect(source)) as db:
        before = publication_state(db)
        objects = {(row[0],row[1]) for row in db.execute('SELECT type,name FROM sqlite_master WHERE sql IS NOT NULL')}
    prepared = migrations.prepare_essential_candidate(source,tmp_path/'video',catalog_importers=[video_essential_importer()])
    assert prepared.imported_items == {'video.essential-records':15}
    with closing(sqlite3.connect(prepared.candidate)) as db:
        assert migrations._state(db,schema_objects=objects,table_names=set(before['table_rows'])) == before
        db.row_factory = _row_factory if factory=='dict' else sqlite3.Row
        store = CategoryStore('video')
        for category in store.listing(db,0,120)['items']:
            assert video_essential_item(db,'category',[category['id']])['id'] == category['id']
        assert store.membership(db,'src-first','Movie/01.mp4')['version'] == 3
        assert db.execute('SELECT count(*) AS n FROM video_category_memberships WHERE active=0').fetchone()['n'] == 1
        assert db.execute('SELECT count(*) AS n FROM kv_items').fetchone()['n'] == 19
        assert db.execute('SELECT count(*) AS n FROM kv_item_relations').fetchone()['n'] == 23
        assert db.execute('SELECT count(*) AS n FROM kv_assets').fetchone()['n'] == 0
        assert db.execute('SELECT future_blob FROM video_categories LIMIT 1').fetchone()['future_blob'] == b'\0\xffpreserve'
    assert migrations.validate_essential_candidate(source,prepared.attempt_directory,catalog_importers=[video_essential_importer()]) == prepared
    assert source.read_bytes() == original


def test_video_essential_source_path_and_historical_receipts_remain_distinct(video_essential_source,tmp_path):
    prepared = migrations.prepare_essential_candidate(video_essential_source,tmp_path/'history',catalog_importers=[video_essential_importer()])
    with closing(sqlite3.connect(prepared.candidate)) as db:
        db.row_factory = sqlite3.Row
        first = video_essential_item(db,'item',['src-first','Movie/01.mp4'])
        assert first != video_essential_item(db,'item',['src-hidden','Movie/01.mp4'])
        assert video_essential_item(db,'item',['src-first',''])
        assert video_essential_item(db,'item',['forgotten-source','Lost.mp4'])
        assert video_essential_item(db,'item',['src-first','Unrecorded.mp4']) is None
        receipt = db.execute("SELECT * FROM video_category_updates WHERE source_id='forgotten-source'").fetchone()
        saved = video_essential_item(db,'category-update',[receipt['request_id']])
        captured = migrations.item_catalog.read_capture(db,item_id=saved['id'],namespace='video.category-receipt',capture_key=receipt['request_id'])
        assert captured['payload'] == dict(source_id=receipt['source_id'],path=receipt['path'],
            expected_version=0,version=1,category_ids_json=receipt['category_ids'])
        assert video_essential_item(db,'category',['removed-category']) is None
        selected = [tuple(row) for row in db.execute("SELECT target_item_id,position FROM kv_item_relations WHERE source_item_id=? AND kind='selected-category'",(saved['id'],))]
        assert selected == [(json.loads(receipt['category_ids'])[1],1)]
        inactive = db.execute('SELECT category_id,source_id,path FROM video_category_memberships WHERE active=0').fetchone()
        membership = video_essential_item(db,'category-membership',list(inactive))
        linked = {row['kind']:row['target_item_id'] for row in db.execute('SELECT kind,target_item_id FROM kv_item_relations WHERE source_item_id=?',(membership['id'],))}
        assert linked == dict(category=inactive['category_id'],item=first['id'])


def test_video_essential_existing_create_retry_membership_conflict_and_sql_filters(video_essential_source,tmp_path):
    from uuid import uuid4
    from fastapi import HTTPException
    from services.library_categories import CategoryStore
    store = CategoryStore('video')
    prepared = migrations.prepare_essential_candidate(video_essential_source,tmp_path/'categories',catalog_importers=[video_essential_importer()])
    original = video_essential_source.read_bytes()
    with closing(sqlite3.connect(video_essential_source)) as source, closing(sqlite3.connect(prepared.candidate)) as db:
        source.row_factory = db.row_factory = sqlite3.Row
        assert store.listing(source,0,120) == store.listing(db,0,120)
        category = db.execute('SELECT * FROM video_categories ORDER BY rowid LIMIT 1').fetchone()
        assert store.create(db,category['name'],category['request_id'])['id'] == category['id']
        before = store.membership(db,'src-first','Movie/01.mp4')
        receipt = db.execute("SELECT * FROM video_category_updates WHERE source_id='src-first' AND path='Movie/01.mp4' AND version=3").fetchone()
        assert store.set_membership(db,'src-first','Movie/01.mp4',before['category_ids'],2,receipt['request_id'],lambda *a: None) == before
        db.execute('CREATE TABLE fixture_files(source_id TEXT,relative_path TEXT)')
        db.executemany('INSERT INTO fixture_files VALUES(?,?)',[('src-first','Movie/01.mp4'),('src-hidden','Movie/01.mp4'),('src-first','Unrecorded.mp4')]); db.commit()
        def filtered(identity):
            predicate,parameters = store.filter_sql(identity,database='main')
            return [tuple(row) for row in db.execute('SELECT f.source_id,f.relative_path FROM fixture_files f WHERE '+predicate,parameters)]
        assert filtered(category['id']) == [('src-first','Movie/01.mp4')]
        assert filtered('default') == [('src-first','Unrecorded.mp4')]
        request = str(uuid4())
        assert store.set_membership(db,'src-first','Movie/01.mp4',[],3,request,lambda *a: None) == dict(version=4,category_ids=[])
        assert store.set_membership(db,'src-first','Movie/01.mp4',[],3,request,lambda *a: None)['version'] == 4
        with pytest.raises(HTTPException) as stale:
            store.set_membership(db,'src-first','Movie/01.mp4',before['category_ids'],3,str(uuid4()),lambda *a: None)
        assert stale.value.status_code == 409
        assert filtered('default') == [('src-first','Movie/01.mp4'),('src-first','Unrecorded.mp4')]
        request = str(uuid4()); created = store.create(db,'New category',request)
        assert store.create(db,'New category',request) == created
        assert store.membership(source,'src-first','Movie/01.mp4') == before
    assert video_essential_source.read_bytes() == original


def test_video_essential_relocated_source_and_renamed_categories_keep_stable_ids(video_essential_source,tmp_path):
    first = migrations.prepare_essential_candidate(video_essential_source,tmp_path/'first',catalog_importers=[video_essential_importer()])
    with closing(sqlite3.connect(first.candidate)) as db: expected = files_essential_identities(db)
    with closing(sqlite3.connect(video_essential_source)) as db:
        db.execute("UPDATE files_sources SET path='/relocated/videos',display_name='Renamed' WHERE source_id='src-first'")
        db.execute("UPDATE video_categories SET name='Changed title',name_key='changed title' WHERE rowid=(SELECT min(rowid) FROM video_categories)"); db.commit()
    second = migrations.prepare_essential_candidate(video_essential_source,tmp_path/'second',catalog_importers=[video_essential_importer()])
    with closing(sqlite3.connect(second.candidate)) as db: assert files_essential_identities(db) == expected
    with pytest.raises(migrations.PreparationError,match='Source changed'):
        migrations.validate_essential_candidate(video_essential_source,first.attempt_directory,catalog_importers=[video_essential_importer()])


@pytest.mark.parametrize('factory',[None,sqlite3.Row,'dict'])
def test_video_essential_direct_retry_and_outer_rollback_preserve_caller_factory(video_essential_source,factory):
    from database_connections import _row_factory
    from modules.video.user_schema import import_catalog_records
    with closing(files_essential_connection(video_essential_source)) as db:
        db.row_factory = _row_factory if factory=='dict' else factory
        before = publication_state(db); db.execute('BEGIN')
        assert import_catalog_records(db) == 15 and db.in_transaction
        captured = publication_state(db)
        assert import_catalog_records(db) == 15 and publication_state(db) == captured
        assert db.row_factory == (_row_factory if factory=='dict' else factory)
        db.rollback(); assert publication_state(db) == before
        with pytest.raises(ValueError,match='caller transaction'): import_catalog_records(db)


@pytest.mark.parametrize('failure',['alias','receipt','relation','historical-category'])
def test_video_essential_late_failure_rolls_back_batch_but_keeps_caller_writes(video_essential_source,monkeypatch,failure):
    from modules.video.user_schema import import_catalog_records
    service = migrations.item_catalog
    with closing(files_essential_connection(video_essential_source)) as db:
        if failure in ('alias','historical-category'):
            category = db.execute('SELECT max(id) FROM video_categories').fetchone()[0] if failure=='alias' else 'removed-category'
            service.ensure_item(db,namespace='video.category',legacy_key=category,owner='foreign',item_type='other')
        if failure in ('receipt','relation'):
            target = 'record_capture' if failure=='receipt' else 'relate_items'
            original = getattr(service,target)
            def fail(*a,**kw):
                value = original(*a,**kw)
                if kw.get('namespace')=='video.category-receipt' or kw.get('kind')=='selected-category': raise InterruptedError('Late Video import failure')
                return value
            monkeypatch.setattr(service,target,fail)
        db.execute('BEGIN'); db.execute("INSERT INTO user_settings(key,value) VALUES('pending','Keep caller write')")
        before = publication_state(db)
        with pytest.raises((ValueError,InterruptedError)): import_catalog_records(db)
        assert db.in_transaction and publication_state(db) == before
        assert db.execute("SELECT value FROM user_settings WHERE key='pending'").fetchone()[0] == 'Keep caller write'


@pytest.mark.parametrize('table,field,value', [('video_categories','id',''),
    ('video_category_memberships','source_id',''),('video_category_memberships','path','bad\0path'),
    ('video_category_versions','path',b'not text'),('video_category_versions','source_id','s'*257),
    ('video_category_updates','path','p'*4097),('video_category_updates','request_id','')],
    ids=['category','source','nul-path','bytes-path','long-source','long-path','request'])
def test_video_essential_invalid_keys_leave_source_untouched(video_essential_source,tmp_path,table,field,value):
    with closing(sqlite3.connect(video_essential_source)) as db:
        db.execute('UPDATE '+table+' SET '+field+'=? WHERE rowid=(SELECT min(rowid) FROM '+table+')',(value,)); db.commit()
    original = video_essential_source.read_bytes()
    with pytest.raises(migrations.PreparationError):
        migrations.prepare_essential_candidate(video_essential_source,tmp_path/'bad',catalog_importers=[video_essential_importer()])
    assert video_essential_source.read_bytes() == original
    with closing(sqlite3.connect(tmp_path/'bad/essential.sqlite')) as db:
        assert db.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 0


@pytest.mark.parametrize('field,value',[('category_ids','{bad'),('category_ids','{}'),('category_ids','[null]'),
    ('category_ids','[""]'),('category_ids',' '*65537),('category_ids',json.dumps(['a']*257)),
    ('expected_version',-1),('version',5)],
    ids=['json','object','null-entry','empty-id','oversized','too-many','negative-version','version-jump'])
def test_video_essential_invalid_receipts_roll_back_all_additions(video_essential_source,field,value):
    from modules.video.user_schema import import_catalog_records
    with closing(files_essential_connection(video_essential_source)) as db:
        db.execute('UPDATE video_category_updates SET '+field+'=? WHERE rowid=(SELECT max(rowid) FROM video_category_updates)',(value,)); db.commit()
        before = publication_state(db); db.execute('BEGIN')
        with pytest.raises(ValueError): import_catalog_records(db)
        assert publication_state(db) == before


@pytest.mark.parametrize('schema,count',[('',0),
    ("CREATE TABLE video_categories(id TEXT PRIMARY KEY); INSERT INTO video_categories VALUES('old-category');",1),
    ("CREATE TABLE video_category_versions(source_id TEXT,path TEXT); INSERT INTO video_category_versions VALUES('missing','old.mp4');",1),
    ('CREATE TABLE video_category_memberships(category_id TEXT,source_id TEXT,path TEXT);',0),
    ("CREATE TABLE video_categories(id TEXT PRIMARY KEY); INSERT INTO video_categories VALUES('old-category'); CREATE TABLE video_category_memberships(category_id TEXT,source_id TEXT,path TEXT); INSERT INTO video_category_memberships VALUES('old-category','missing','old.mp4');",2)],
    ids=['absent','category-only','version-only','empty-memberships','legacy-membership'])
def test_video_essential_old_and_absent_owner_schemas(schema,count):
    from modules.video.user_schema import import_catalog_records
    with closing(sqlite3.connect(':memory:')) as db:
        db.executescript(schema); migrations.item_catalog.ensure_catalog_schema(db); db.execute('BEGIN')
        assert import_catalog_records(db) == count


@pytest.mark.parametrize('schema',['CREATE TABLE video_categories(other TEXT);',
    "CREATE TABLE video_category_versions(source_id TEXT,path TEXT); INSERT INTO video_category_versions VALUES('a','b'),('a','b');",
    'CREATE TABLE video_category_updates(request_id TEXT PRIMARY KEY);',
    "CREATE TABLE video_category_memberships(category_id TEXT,source_id TEXT,path TEXT); INSERT INTO video_category_memberships VALUES('absent','a','b');"],
    ids=['missing-column','duplicate-key','partial-receipt','orphan-parent'])
def test_video_essential_unsupported_or_orphan_schema_refuses(schema):
    from modules.video.user_schema import import_catalog_records
    with closing(sqlite3.connect(':memory:')) as db:
        db.executescript(schema); migrations.item_catalog.ensure_catalog_schema(db); db.execute('BEGIN')
        with pytest.raises(ValueError): import_catalog_records(db)
        assert db.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 0


def test_video_essential_maximum_compound_path_keys_are_preserved_in_bounded_aliases():
    from modules.video.user_schema import publish,import_catalog_records
    source,path = 's'*256,'p'*4096
    with closing(sqlite3.connect(':memory:')) as db:
        publish(db); db.execute('INSERT INTO video_category_versions VALUES(?,?,1)',(source,path)); db.commit()
        migrations.item_catalog.ensure_catalog_schema(db); db.execute('BEGIN')
        assert import_catalog_records(db) == 1
        assert video_essential_item(db,'item',[source,path])
        assert max(row[0] for row in db.execute('SELECT length(legacy_key) FROM kv_legacy_keys')) < 4096


def test_video_essential_combined_files_manga_snapshot_keeps_owners_separate(video_essential_source,tmp_path):
    from modules.manga import user_schema as manga_schema
    from services.library_categories import CategoryStore
    from uuid import uuid4
    with closing(sqlite3.connect(video_essential_source)) as db:
        db.row_factory = sqlite3.Row; manga_schema.publish(db)
        CategoryStore('manga').create(db,'Favorites',str(uuid4()))
        db.execute("INSERT INTO manga_reading_sessions VALUES('src-first','Movie/01.mp4','folder',1)"); db.commit()
    original = video_essential_source.read_bytes()
    importers = [files_essential_importer(),manga_essential_importer(),video_essential_importer()]
    prepared = migrations.prepare_essential_candidate(video_essential_source,tmp_path/'combined',catalog_importers=importers)
    assert prepared.imported_items == {'files.annotations':5,'manga.essential-records':2,'video.essential-records':15}
    restored = tmp_path/'restored.sqlite'; migrations.snapshot_user_database(prepared.candidate,restored)
    with closing(sqlite3.connect(prepared.candidate)) as old, closing(sqlite3.connect(restored)) as db:
        assert publication_state(db) == publication_state(old)
        assert db.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 27
        assert video_essential_item(db,'item',['src-first','Movie/01.mp4'])['id'] != manga_essential_item(db,'chapter',['src-first','Movie/01.mp4','folder'])['id']
        assert db.execute('SELECT count(*) FROM files_annotations').fetchone()[0] == 5
        db.row_factory = sqlite3.Row
        assert CategoryStore('video').membership(db,'src-first','Movie/01.mp4')['version'] == 3
    assert migrations.prepare_essential_candidate(video_essential_source,prepared.attempt_directory,catalog_importers=importers) == prepared
    assert video_essential_source.read_bytes() == original


def test_video_essential_changed_historical_receipt_refuses_without_replacing_facts(video_essential_source):
    from modules.video.user_schema import import_catalog_records
    with closing(files_essential_connection(video_essential_source)) as db:
        db.execute('BEGIN'); import_catalog_records(db); db.commit()
        db.execute("UPDATE video_category_updates SET category_ids='[]' WHERE source_id='forgotten-source'"); db.commit()
        before = publication_state(db); db.execute('BEGIN')
        with pytest.raises(migrations.item_catalog.CatalogConflict): import_catalog_records(db)
        assert publication_state(db) == before


def test_video_essential_import_never_opens_media_index_runtime_or_network(video_essential_source,monkeypatch):
    from modules.video import user_schema
    from files_base import index
    import database,socket
    with closing(files_essential_connection(video_essential_source)) as db:
        def blocked(*a,**kw): raise AssertionError('No media, index, runtime or network access')
        with monkeypatch.context() as scoped:
            for name in ('open','stat','resolve'): scoped.setattr(Path,name,blocked)
            scoped.setattr(index,'open_index',blocked); scoped.setattr(database,'get_user_db',blocked)
            scoped.setattr(socket,'create_connection',blocked); scoped.setattr(user_schema,'publish',blocked)
            db.execute('BEGIN'); assert user_schema.import_catalog_records(db) == 15; db.commit()


def test_video_essential_fresh_import_does_not_load_player_or_runtime():
    code = r'''
import builtins,sqlite3,sys
sys.path.insert(0,sys.argv[1])
original=builtins.__import__
def guarded(name,globals=None,locals=None,fromlist=(),level=0):
    if level==0 and (name in ('config','database','lifecycle','suite_modules','modules.video.router','files_base.index') or name.startswith(('modules.manga','modules.youtube','modules.twitter','yt_dlp','PIL'))):
        raise AssertionError('Unexpected runtime/engine import: '+name)
    return original(name,globals,locals,fromlist,level)
builtins.__import__=guarded
from modules.video.user_schema import import_catalog_records
from services.item_catalog import ensure_catalog_schema
with sqlite3.connect(':memory:') as db:
    db.executescript("CREATE TABLE video_category_versions(source_id TEXT,path TEXT); INSERT INTO video_category_versions VALUES('missing','old.mp4');")
    ensure_catalog_schema(db); db.execute('BEGIN')
    assert import_catalog_records(db)==1
    db.commit()
assert not any(name in sys.modules for name in ('config','database','lifecycle','modules.video.router','files_base.index'))
'''
    options = dict(capture_output=True,timeout=30)
    if os.name=='nt': options['creationflags'] = subprocess.CREATE_NO_WINDOW
    completed = subprocess.run([sys.executable,'-c',code,str(Path(__file__).resolve().parents[1]/'backend')],**options)
    assert completed.returncode == 0, completed.stderr.decode('utf-8','replace')


@pytest.fixture
def music_preservation_source(files_essential_source, tmp_path):
    from types import SimpleNamespace
    from files_base import annotations
    from tests.test_ost_module import seed_publisher_music, publisher_music_tree
    root = tmp_path / '[Publisher] Original release'
    paths = seed_publisher_music(root)
    digest = hashlib.md5((root / paths[0]).read_bytes()).hexdigest()
    with closing(sqlite3.connect(files_essential_source)) as db:
        db.row_factory = sqlite3.Row
        db.execute("UPDATE files_sources SET path=?,role='ost' WHERE source_id='src-first'", (str(root),))
        db.execute("UPDATE files_sources SET role='ost' WHERE source_id='src-hidden'"); db.commit()
        note = annotations.upsert_annotation(db, subject_kind='file', content_hash=digest,
            source_id='src-first', relative_path=paths[0], description='My publisher-album note', author='My override')
    before = publisher_music_tree(root)
    yield SimpleNamespace(source=files_essential_source, root=root, paths=paths, note_id=note.id, digest=digest)
    assert publisher_music_tree(root) == before


def test_music_preservation_essential_snapshot_keeps_notes_without_catalog_promotion(music_preservation_source, tmp_path, monkeypatch):
    from files_base import annotations
    from modules import ost
    case = music_preservation_source
    original_bytes = case.source.read_bytes()
    with closing(sqlite3.connect(case.source)) as db:
        before = publication_state(db)
        objects = {(row[0], row[1]) for row in db.execute('SELECT type,name FROM sqlite_master WHERE sql IS NOT NULL')}
    original_open = Path.open
    def no_library_reads(path, *args, **kwargs):
        assert path != case.root and case.root not in path.parents, 'Essential preparation must not read Music originals'
        return original_open(path, *args, **kwargs)
    with monkeypatch.context() as scoped:
        scoped.setattr(Path, 'open', no_library_reads)
        prepared = migrations.prepare_essential_candidate(case.source, tmp_path / 'essential', catalog_importers=[files_essential_importer()])
    assert prepared.imported_items == {'files.annotations': 6} and prepared.registered_libraries == 2
    restored = tmp_path / 'restored.sqlite'
    migrations.snapshot_user_database(prepared.candidate, restored)
    with closing(sqlite3.connect(prepared.candidate)) as candidate, closing(sqlite3.connect(restored)) as db:
        assert publication_state(db) == publication_state(candidate)
        assert migrations._state(db, schema_objects=objects, table_names=set(before['table_rows'])) == before
        db.row_factory = sqlite3.Row
        note = annotations.get_annotation(db, content_hash=case.digest, source_id='src-first', relative_path=case.paths[0])
        assert (note.id, note.description, note.author) == (case.note_id, 'My publisher-album note', 'My override')
        assert migrations.item_catalog.item_for_legacy(db, namespace='files.annotation', legacy_key=str(note.id))['owner'] == 'files'
        assert {row['owner'] for row in db.execute('SELECT owner FROM kv_items')} == {'files'}
        tables = {row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not tables.intersection({'ost_catalog', 'files_audio_metadata', 'music_tracks', 'music_albums'})
        assert db.execute("SELECT library_id FROM kv_library_sources WHERE namespace='files.source' AND legacy_key='src-first'").fetchone()['library_id'] == 'src-first'
        assert db.execute("SELECT path,role FROM files_sources WHERE source_id='src-first'").fetchone()['role'] == 'ost'
    descriptor = ost.descriptor(tmp_path / 'app-data', 'test')
    assert descriptor.user_schema_provider is None and descriptor.backup_components() == ()
    assert migrations.validate_essential_candidate(case.source, prepared.attempt_directory, catalog_importers=[files_essential_importer()]) == prepared
    assert case.source.read_bytes() == original_bytes


@pytest.mark.parametrize('hazard', ['hidden', 'missing', 'child'])
def test_music_preservation_restored_audio_ranges_keep_current_source_guards(music_preservation_source, tmp_path, monkeypatch, hazard):
    from contextlib import contextmanager
    from fastapi import FastAPI
    from routers import files
    from files_base import sources
    import config
    case = music_preservation_source
    original_bytes = case.source.read_bytes()
    prepared = migrations.prepare_essential_candidate(case.source, tmp_path / 'essential', catalog_importers=[files_essential_importer()])
    restored = tmp_path / 'restored.sqlite'; migrations.snapshot_user_database(prepared.candidate, restored)
    @contextmanager
    def connect():
        with closing(sqlite3.connect(restored)) as connection:
            connection.row_factory = sqlite3.Row
            yield connection
    monkeypatch.setattr(files, 'get_user_db', connect)
    monkeypatch.setattr(config, 'USER_DB_PATH', restored)
    monkeypatch.setattr(config, 'SUITE_HOME', tmp_path / 'app-data')
    monkeypatch.setattr(config, 'FILES_DB_PATH', tmp_path / 'absent-index.sqlite')
    app = FastAPI(); app.include_router(files.router)
    original_audio = (case.root / case.paths[0]).read_bytes()
    status, headers, body = saved_file_http(app, 'src-first', case.paths[0])
    assert status == 200 and body == original_audio and headers[b'content-type'].startswith(b'audio/')
    status, headers, body = saved_file_http(app, 'src-first', case.paths[0], 'bytes=5-16')
    assert status == 206 and body == original_audio[5:17]
    assert headers[b'content-range'] == f'bytes 5-16/{len(original_audio)}'.encode()
    assert headers[b'accept-ranges'] == b'bytes' and headers[b'x-content-type-options'] == b'nosniff'
    with connect() as connection:
        if hazard == 'hidden': sources.update_source(connection, 'src-first', visible=False)
        elif hazard == 'missing':
            connection.execute("UPDATE files_sources SET path=? WHERE source_id='src-first'", (str(case.root.with_name('Missing collection')),)); connection.commit()
        else:
            child = sources.register_source(connection, case.root / 'Disc A', 'Hidden child', role='files')
            sources.update_source(connection, child.source_id, visible=False)
    assert saved_file_http(app, 'src-first', case.paths[0], 'bytes=5-16')[0] == (403 if hazard == 'child' else 404)
    assert not (tmp_path / 'absent-index.sqlite').exists()
    assert case.source.read_bytes() == original_bytes


@pytest.fixture
def language_essential_source(files_essential_source, tmp_path):
    from types import SimpleNamespace
    from uuid import uuid4
    import unicodedata
    from files_base import annotations
    from modules.language import user_schema, vocabulary, study
    notes = tmp_path / 'Language originals'; notes.mkdir()
    name = 'Note 한글.md'; original = '# Original\r\n한글 **notes**\r\n'.encode('utf-8')
    (notes / name).write_bytes(original)
    history = tmp_path / 'Language history/revisions/old-identity/complete/old-session.md'
    history.parent.mkdir(parents=True); history.write_bytes(b'Original preserved history\r\n')
    with closing(sqlite3.connect(files_essential_source)) as db:
        db.row_factory = sqlite3.Row; user_schema.publish(db)
        db.execute("UPDATE files_sources SET path=?,role='language' WHERE source_id='src-first'", (str(notes),))
        db.execute("UPDATE files_sources SET role='language' WHERE source_id='src-hidden'")
        db.execute("INSERT INTO language_study_preferences VALUES(1,'native','english',1,20)"); db.commit()
        study.save(db, study.StudyPreferences(category='date_time',answer_language='korean',minimum=2,maximum=200000))
        content = dict(korean=unicodedata.normalize('NFD','  한글  '), pronunciation='  han-geul\t', english='  Meaning\r\n<script>literal</script>  ')
        first = vocabulary.create(db, vocabulary.VocabularyCreate(request_id=uuid4(), **content))
        vocabulary.create(db, vocabulary.VocabularyCreate(request_id=uuid4(), **content))
        third = vocabulary.create(db, vocabulary.VocabularyCreate(request_id=uuid4(), korean='옛말', english='old word'))
        vocabulary.edit(db, first.id, vocabulary.VocabularyEdit(request_id=uuid4(), revision=0,
            korean='  첫 편집 ', pronunciation='edited\t', english='first edit\r\n'))
        vocabulary.edit(db, first.id, vocabulary.VocabularyEdit(request_id=uuid4(), revision=1,
            korean='둘째 편집', pronunciation='', english='second edit'))
        vocabulary.archive(db, third.id, vocabulary.VocabularyMutation(request_id=uuid4(), revision=0))
        db.execute('ALTER TABLE language_vocabulary ADD COLUMN future_blob BLOB')
        db.execute('UPDATE language_vocabulary SET future_blob=?',(b'\0\xffunknown',)); db.commit()
        note = annotations.upsert_annotation(db, subject_kind='file', content_hash=hashlib.md5(original).hexdigest(),
            source_id='src-first',relative_path=name,description='Note reference kept',author='Local author')
    return SimpleNamespace(source=files_essential_source, notes=notes, name=name, original=original, history=history, note_id=note.id)


def language_essential_importer(callback=None):
    from modules.language.user_schema import import_catalog_records
    return migrations.CatalogImporter('language.essential-records',1,callback or import_catalog_records)


def language_essential_item(db,kind,key):
    cursor = db.cursor(); cursor.row_factory = sqlite3.Row
    try:
        for row in cursor.execute("SELECT i.id,i.owner,i.item_type FROM kv_items i JOIN kv_item_captures c ON c.item_id=i.id "
                "WHERE i.owner='language' AND i.item_type=? AND c.namespace='language.record' AND c.capture_key='identity'",(kind,)):
            captured = migrations.item_catalog.read_capture(db,item_id=row['id'],namespace='language.record',capture_key='identity')
            if captured['payload'] == dict(kind=kind,key=key): return dict(row)
        return None
    finally:
        cursor.close()


@pytest.mark.parametrize('factory',[sqlite3.Row,'dict'])
def test_language_essential_snapshot_preserves_ids_text_history_and_preferences(language_essential_source,tmp_path,factory):
    from database_connections import _row_factory
    from modules.language import vocabulary,study
    case = language_essential_source; original = case.source.read_bytes()
    with closing(sqlite3.connect(case.source)) as db:
        before = publication_state(db)
        objects = {(row[0],row[1]) for row in db.execute('SELECT type,name FROM sqlite_master WHERE sql IS NOT NULL')}
    prepared = migrations.prepare_essential_candidate(case.source,tmp_path/'language',catalog_importers=[language_essential_importer()])
    assert prepared.imported_items == {'language.essential-records':8}
    with closing(sqlite3.connect(case.source)) as source, closing(sqlite3.connect(prepared.candidate)) as db:
        assert migrations._state(db,schema_objects=objects,table_names=set(before['table_rows'])) == before
        source.row_factory = db.row_factory = _row_factory if factory=='dict' else sqlite3.Row
        assert vocabulary.listing(db,0,100) == vocabulary.listing(source,0,100)
        assert vocabulary.listing(db,0,100).total == 2
        assert study.read(db) == study.read(source) and study.read(db).category == 'date_time'
        for row in db.execute('SELECT id,request_id FROM language_vocabulary'):
            assert language_essential_item(db,'vocabulary',[row['id']])['id'] == row['id']
            assert migrations.item_catalog.item_for_legacy(db,namespace='language.request',legacy_key=row['request_id'])['id'] == row['id']
        for row in db.execute('SELECT entry_id,revision,request_id FROM language_vocabulary_revisions'):
            saved = language_essential_item(db,'vocabulary-revision',[row['entry_id'],row['revision']])
            assert migrations.item_catalog.item_for_legacy(db,namespace='language.request',legacy_key=row['request_id'])['id'] == saved['id']
            assert db.execute("SELECT target_item_id FROM kv_item_relations WHERE source_item_id=? AND kind='vocabulary'",(saved['id'],)).fetchone()['target_item_id'] == row['entry_id']
        assert language_essential_item(db,'study-preferences',['language_study_preferences',1]) != language_essential_item(db,'study-preferences',['language_study_preferences_v2',1])
        assert db.execute('SELECT count(*) AS n FROM kv_items').fetchone()['n'] == 8
        assert db.execute('SELECT count(*) AS n FROM kv_item_captures').fetchone()['n'] == 14
        assert db.execute('SELECT count(*) AS n FROM kv_assets').fetchone()['n'] == 0
        assert db.execute('SELECT future_blob FROM language_vocabulary LIMIT 1').fetchone()['future_blob'] == b'\0\xffunknown'
    assert migrations.validate_essential_candidate(case.source,prepared.attempt_directory,catalog_importers=[language_essential_importer()]) == prepared
    assert case.source.read_bytes() == original and (case.notes/case.name).read_bytes() == case.original


def test_language_essential_exact_receipts_keep_original_and_revised_authored_text(language_essential_source,tmp_path):
    import unicodedata
    case = language_essential_source
    prepared = migrations.prepare_essential_candidate(case.source,tmp_path/'receipts',catalog_importers=[language_essential_importer()])
    with closing(sqlite3.connect(prepared.candidate)) as db:
        db.row_factory = sqlite3.Row
        originals = db.execute('SELECT * FROM language_vocabulary ORDER BY rowid').fetchall()
        assert originals[0]['korean'] == originals[1]['korean'] and originals[0]['id'] != originals[1]['id']
        assert originals[0]['korean'] != unicodedata.normalize('NFC',originals[0]['korean'])
        for table,kind,operation in [('language_vocabulary','vocabulary','create'),('language_vocabulary_revisions','vocabulary-revision',None)]:
            for row in db.execute('SELECT * FROM '+table):
                key = [row['id']] if operation else [row['entry_id'],row['revision']]
                item = language_essential_item(db,kind,key)
                captured = migrations.item_catalog.read_capture(db,item_id=item['id'],namespace='language.receipt',capture_key=row['request_id'])['payload']
                assert all(captured[field] == row[field] for field in ('request_id','korean','pronunciation','english'))
                assert captured['operation'] == (operation or row['kind'])
        assert {row['kind'] for row in db.execute('SELECT kind FROM language_vocabulary_revisions')} == {'edit','archive'}


def test_language_essential_existing_vocabulary_retries_edits_and_conflicts(language_essential_source,tmp_path):
    from uuid import UUID,uuid4
    from modules.language import vocabulary
    case = language_essential_source
    prepared = migrations.prepare_essential_candidate(case.source,tmp_path/'writes',catalog_importers=[language_essential_importer()])
    with closing(sqlite3.connect(prepared.candidate)) as db:
        db.row_factory = sqlite3.Row
        first = db.execute('SELECT * FROM language_vocabulary ORDER BY rowid LIMIT 1').fetchone()
        create = vocabulary.VocabularyCreate(**{field:first[field] for field in ('request_id','korean','pronunciation','english')})
        assert str(vocabulary.create(db,create).id) == first['id']
        old = db.execute("SELECT * FROM language_vocabulary_revisions WHERE entry_id=? AND revision=1",(first['id'],)).fetchone()
        edit = vocabulary.VocabularyEdit(revision=0,**{field:old[field] for field in ('request_id','korean','pronunciation','english')})
        assert vocabulary.edit(db,UUID(first['id']),edit).revision == 1
        assert vocabulary.read(db,UUID(first['id'])).revision == 2
        with pytest.raises(vocabulary.RequestConflict):
            vocabulary.edit(db,UUID(first['id']),vocabulary.VocabularyEdit(request_id=uuid4(),revision=1,korean='stale',english='keep current'))
        with pytest.raises(vocabulary.RequestConflict):
            vocabulary.archive(db,UUID(first['id']),vocabulary.VocabularyMutation(request_id=UUID(first['request_id']),revision=2))
        archived = db.execute("SELECT * FROM language_vocabulary_revisions WHERE kind='archive'").fetchone()
        assert vocabulary.archive(db,UUID(archived['entry_id']),vocabulary.VocabularyMutation(request_id=UUID(archived['request_id']),revision=0)).archived
        updated = vocabulary.edit(db,UUID(first['id']),vocabulary.VocabularyEdit(request_id=uuid4(),revision=2,korean='새 말',english='new meaning'))
        assert updated.revision == 3
        db.execute('BEGIN'); assert language_essential_importer().import_records(db) == 9; db.commit()
        assert language_essential_item(db,'vocabulary',[first['id']])['id'] == first['id']


def test_language_essential_preference_fallback_and_mutable_values_keep_identity(language_essential_source,tmp_path):
    from modules.language import study,user_schema
    case = language_essential_source
    prepared = migrations.prepare_essential_candidate(case.source,tmp_path/'preferences',catalog_importers=[language_essential_importer()])
    with closing(sqlite3.connect(prepared.candidate)) as db:
        db.row_factory = sqlite3.Row
        expected = language_essential_item(db,'study-preferences',['language_study_preferences_v2',1])
        db.execute('DELETE FROM language_study_preferences_v2'); db.commit()
        assert study.read(db).category == 'native' and study.read(db).maximum == 20
        changed = study.StudyPreferences(category='minutes',answer_language='english',minimum=3,maximum=59)
        assert study.save(db,changed) == changed
        db.execute('BEGIN'); assert user_schema.import_catalog_records(db) == 8; db.commit()
        assert language_essential_item(db,'study-preferences',['language_study_preferences_v2',1]) == expected
        assert study.read(db) == changed


@pytest.mark.parametrize('factory',[None,sqlite3.Row,'dict'])
def test_language_essential_exact_import_retry_and_caller_rollback(language_essential_source,factory):
    from database_connections import _row_factory
    from modules.language.user_schema import import_catalog_records
    with closing(files_essential_connection(language_essential_source.source)) as db:
        db.row_factory = _row_factory if factory=='dict' else factory
        before = publication_state(db); db.execute('BEGIN')
        assert import_catalog_records(db) == 8 and db.in_transaction
        captured = publication_state(db)
        assert import_catalog_records(db) == 8 and publication_state(db) == captured
        assert db.row_factory == (_row_factory if factory=='dict' else factory)
        db.rollback(); assert publication_state(db) == before
        with pytest.raises(ValueError,match='caller transaction'): import_catalog_records(db)


@pytest.mark.parametrize('failure',['entry-alias','request-alias','permanent-id','receipt','relation'])
def test_language_essential_late_failure_keeps_existing_catalog_and_caller_writes(language_essential_source,monkeypatch,failure):
    from modules.language.user_schema import import_catalog_records
    service = migrations.item_catalog
    with closing(files_essential_connection(language_essential_source.source)) as db:
        if failure in ('entry-alias','request-alias','permanent-id'):
            row = db.execute('SELECT id,request_id FROM language_vocabulary ORDER BY id DESC LIMIT 1').fetchone()
            namespace,key = ('language.vocabulary',row[0]) if failure=='entry-alias' else ('language.request',row[1])
            service.ensure_item(db,namespace='foreign.record' if failure=='permanent-id' else namespace,
                legacy_key=key,owner='foreign',item_type='other',item_id=row[0] if failure=='permanent-id' else None)
        else:
            target = 'record_capture' if failure=='receipt' else 'relate_items'; original = getattr(service,target)
            def interrupted(*args,**kwargs):
                result = original(*args,**kwargs)
                if kwargs.get('kind')=='vocabulary' or kwargs.get('namespace')=='language.receipt' and kwargs['payload']['operation']=='edit':
                    raise InterruptedError('Interrupted Language import')
                return result
            monkeypatch.setattr(service,target,interrupted)
        db.execute('BEGIN'); db.execute("INSERT INTO user_settings(key,value) VALUES('pending','Keep caller write')")
        before = publication_state(db)
        with pytest.raises((ValueError,InterruptedError)): import_catalog_records(db)
        assert db.in_transaction and publication_state(db) == before


@pytest.mark.parametrize('table,field,value',[('language_vocabulary','id',''),('language_vocabulary','request_id','bad\0request'),
    ('language_vocabulary','english',b'not JSON text'),('language_vocabulary_revisions','entry_id','absent'),
    ('language_vocabulary_revisions','revision',0),('language_vocabulary_revisions','kind','other'),
    ('language_vocabulary_revisions','request_id','r'*257),('language_study_preferences','id',2)],
    ids=['empty-id','nul-request','invalid-text','orphan','zero-revision','kind','long-request','preference-id'])
def test_language_essential_invalid_keys_or_receipts_preserve_source(language_essential_source,tmp_path,table,field,value):
    case = language_essential_source
    with closing(sqlite3.connect(case.source)) as db:
        db.execute('PRAGMA ignore_check_constraints=ON')
        db.execute('UPDATE '+table+' SET '+field+'=? WHERE rowid=(SELECT min(rowid) FROM '+table+')',(value,)); db.commit()
    original = case.source.read_bytes()
    with pytest.raises(migrations.PreparationError):
        migrations.prepare_essential_candidate(case.source,tmp_path/'bad',catalog_importers=[language_essential_importer()])
    assert case.source.read_bytes() == original
    candidate = tmp_path/'bad/essential.sqlite'
    if candidate.exists():
        with closing(sqlite3.connect(candidate)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='kv_items'").fetchone():
                assert db.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 0
    with closing(files_essential_connection(case.source)) as db:
        before = publication_state(db); db.execute('BEGIN')
        with pytest.raises(ValueError): language_essential_importer().import_records(db)
        assert publication_state(db) == before


def test_language_essential_reused_cross_operation_request_refuses_atomically(language_essential_source):
    from modules.language.user_schema import import_catalog_records
    with closing(files_essential_connection(language_essential_source.source)) as db:
        request = db.execute('SELECT request_id FROM language_vocabulary ORDER BY rowid LIMIT 1').fetchone()[0]
        db.execute('UPDATE language_vocabulary_revisions SET request_id=? WHERE rowid=(SELECT max(rowid) FROM language_vocabulary_revisions)',(request,)); db.commit()
        before = publication_state(db); db.execute('BEGIN')
        with pytest.raises(migrations.item_catalog.CatalogConflict): import_catalog_records(db)
        assert publication_state(db) == before


@pytest.mark.parametrize('table',['language_vocabulary','language_vocabulary_revisions'])
def test_language_essential_authored_receipt_cannot_be_rewritten(language_essential_source,table):
    from modules.language.user_schema import import_catalog_records
    with closing(files_essential_connection(language_essential_source.source)) as db:
        db.execute('BEGIN'); import_catalog_records(db); db.commit()
        db.execute('UPDATE '+table+" SET english='changed history' WHERE rowid=(SELECT min(rowid) FROM "+table+')'); db.commit()
        before = publication_state(db); db.execute('BEGIN')
        with pytest.raises(migrations.item_catalog.CatalogConflict): import_catalog_records(db)
        assert publication_state(db) == before


@pytest.mark.parametrize('schema,count',[('',0),
    ('CREATE TABLE language_study_preferences(id INTEGER); INSERT INTO language_study_preferences VALUES(1);',1),
    ('CREATE TABLE language_study_preferences_v2(id INTEGER); INSERT INTO language_study_preferences_v2 VALUES(1);',1),
    ("CREATE TABLE language_vocabulary(id TEXT,request_id TEXT,korean TEXT,pronunciation TEXT,english TEXT,created_at TEXT); INSERT INTO language_vocabulary VALUES('old-entry','old-request','옛말','','old word','original date');",1)],
    ids=['absent','old-preferences','new-preferences','old-vocabulary'])
def test_language_essential_absent_and_old_owner_schemas(schema,count):
    from modules.language.user_schema import import_catalog_records
    with closing(sqlite3.connect(':memory:')) as db:
        db.executescript(schema); migrations.item_catalog.ensure_catalog_schema(db); db.execute('BEGIN')
        assert import_catalog_records(db) == count


@pytest.mark.parametrize('schema',['CREATE TABLE language_vocabulary(id TEXT);',
    'CREATE TABLE language_study_preferences(id INTEGER); INSERT INTO language_study_preferences VALUES(1),(1);',
    "CREATE TABLE language_vocabulary_revisions(entry_id TEXT,revision INTEGER,request_id TEXT,kind TEXT,korean TEXT,pronunciation TEXT,english TEXT,recorded_at TEXT); INSERT INTO language_vocabulary_revisions VALUES('absent',1,'request','edit','말','','word','original date');"],
    ids=['partial-schema','duplicate-key','orphan-revision'])
def test_language_essential_bad_schema_or_parent_refuses_without_partial_records(schema):
    from modules.language.user_schema import import_catalog_records
    with closing(sqlite3.connect(':memory:')) as db:
        db.executescript(schema); migrations.item_catalog.ensure_catalog_schema(db); db.execute('BEGIN')
        with pytest.raises(ValueError): import_catalog_records(db)
        assert db.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 0


def test_language_essential_combined_restore_keeps_note_files_and_references(language_essential_source,tmp_path):
    from files_base import sources,annotations
    from modules.language import documents,vocabulary
    case = language_essential_source; original_db = case.source.read_bytes(); history = case.history.read_bytes()
    importers = [files_essential_importer(),language_essential_importer()]
    prepared = migrations.prepare_essential_candidate(case.source,tmp_path/'combined',catalog_importers=importers)
    assert prepared.imported_items == {'files.annotations':6,'language.essential-records':8}
    restored = tmp_path/'restored.sqlite'; migrations.snapshot_user_database(prepared.candidate,restored)
    with closing(sqlite3.connect(prepared.candidate)) as old, closing(sqlite3.connect(restored)) as db:
        assert publication_state(db) == publication_state(old)
        db.row_factory = sqlite3.Row
        source = sources.get_source(db,'src-first')
        saved = documents.read_document(source,tmp_path/'app-data',case.name)
        assert saved['content'].encode('utf-8') == case.original
        assert saved['revision'] == hashlib.sha256(case.original).hexdigest()
        note = annotations.get_annotation(db,content_hash=hashlib.md5(case.original).hexdigest(),source_id='src-first',relative_path=case.name)
        assert note.id == case.note_id and note.relative_path == case.name
        assert vocabulary.listing(db,0,100).total == 2
        assert db.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 14
        assert not db.execute("SELECT 1 FROM kv_items WHERE owner='language' AND item_type='note'").fetchone()
    assert migrations.prepare_essential_candidate(case.source,prepared.attempt_directory,catalog_importers=importers) == prepared
    assert case.source.read_bytes() == original_db and case.history.read_bytes() == history


def test_language_essential_relocation_changes_no_vocabulary_or_note_reference_ids(language_essential_source,tmp_path):
    import shutil
    from files_base import sources
    from modules.language import documents
    case = language_essential_source
    first = migrations.prepare_essential_candidate(case.source,tmp_path/'first',catalog_importers=[language_essential_importer()])
    with closing(sqlite3.connect(first.candidate)) as db: expected = files_essential_identities(db)
    relocated = tmp_path/'Relocated notes'; shutil.copytree(case.notes,relocated)
    with closing(sqlite3.connect(case.source)) as db:
        db.execute("UPDATE files_sources SET path=?,display_name='Renamed notes' WHERE source_id='src-first'",(str(relocated),)); db.commit()
    second = migrations.prepare_essential_candidate(case.source,tmp_path/'second',catalog_importers=[language_essential_importer()])
    with closing(sqlite3.connect(second.candidate)) as db:
        assert files_essential_identities(db) == expected
        db.row_factory = sqlite3.Row
        assert documents.read_document(sources.get_source(db,'src-first'),tmp_path/'app-data',case.name)['content'].encode('utf-8') == case.original
    assert (case.notes/case.name).read_bytes() == case.original


def test_language_essential_fresh_import_has_no_runtime_or_markdown_io():
    code = r'''
import builtins,sqlite3,sys
sys.path.insert(0,sys.argv[1])
original=builtins.__import__
def guarded(name,globals=None,locals=None,fromlist=(),level=0):
    if level==0 and (name in ('config','database','lifecycle','suite_modules','modules.language.documents','modules.language.router','modules.language.study','modules.language.vocabulary') or name.startswith(('modules.manga','modules.youtube','modules.twitter','PIL'))):
        raise AssertionError('Unexpected runtime/engine import: '+name)
    return original(name,globals,locals,fromlist,level)
builtins.__import__=guarded
from modules.language.user_schema import import_catalog_records
from services.item_catalog import ensure_catalog_schema
with sqlite3.connect(':memory:') as db:
    db.executescript('CREATE TABLE language_study_preferences(id INTEGER); INSERT INTO language_study_preferences VALUES(1);')
    ensure_catalog_schema(db); db.execute('BEGIN'); assert import_catalog_records(db)==1; db.commit()
assert not any(name in sys.modules for name in ('config','database','lifecycle','modules.language.documents','modules.language.router','modules.language.study','modules.language.vocabulary'))
'''
    options = dict(capture_output=True,timeout=30)
    if os.name=='nt': options['creationflags'] = subprocess.CREATE_NO_WINDOW
    completed = subprocess.run([sys.executable,'-c',code,str(Path(__file__).resolve().parents[1]/'backend')],**options)
    assert completed.returncode == 0, completed.stderr.decode('utf-8','replace')


def combined_essential_importers():
    return [files_essential_importer(),danbooru_essential_importer(),manga_essential_importer(),
            video_essential_importer(),language_essential_importer(),twitter_receipt_importer(),library_importer()]


COMBINED_ESSENTIAL_COUNTS = {'files.annotations':6,'danbooru.essential-records':31,
    'manga.essential-records':26,'video.essential-records':15,'language.essential-records':8,
    'twitter.saved-receipts':6,'youtube.saved-library':2}


@pytest.fixture
def combined_essential_source(request,tmp_path):
    from types import SimpleNamespace
    case = request.getfixturevalue('language_essential_source')
    for name in ('danbooru_essential_source','manga_essential_source','video_essential_source',
                 'captured_youtube_source','twitter_receipt_source'):
        assert request.getfixturevalue(name) == case.source
    music = tmp_path/'Publisher Music/Original edition/Disc A'
    music.mkdir(parents=True)
    (music/'01 - Original.flac').write_bytes(b'Publisher audio fixture: preserve without parsing')
    (music.parent/'cover.jpg').write_bytes(b'Publisher cover fixture: preserve without decoding')
    (music.parent/'release.cue').write_bytes(b'Original publisher cue\r\n')
    with closing(sqlite3.connect(case.source)) as db:
        db.execute("UPDATE files_sources SET path=?,role='language' WHERE source_id='src-first'",(str(case.notes),))
        db.execute("INSERT INTO files_sources(source_id,path,display_name,role,visible) VALUES('src-music',?,'Publisher albums','ost',1)",(str(music.parent),)); db.commit()
    originals = {path:path.read_bytes() for path in [case.notes/case.name,case.history,*music.parent.rglob('*')] if path.is_file()}
    yield SimpleNamespace(source=case.source,language=case,music=music.parent)
    assert all(path.read_bytes() == data for path,data in originals.items())
    assert {path for path in music.parent.rglob('*') if path.is_file()} == {path for path in originals if path.is_relative_to(music.parent)}


def combined_essential_projection(db):
    from modules.language import vocabulary,study
    from modules.manga import progress
    from modules.youtube import playlists
    from modules.danbooru.artist_profiles import list_artist_profile_assets_from_conn
    from services.library_categories import CategoryStore
    return dict(vocabulary=vocabulary.listing(db,0,100).model_dump(mode='json'),
        study=study.read(db).model_dump(),
        manga_categories=CategoryStore('manga').listing(db,0,120),
        video_categories=CategoryStore('video').listing(db,0,120),
        manga_membership=CategoryStore('manga').membership(db,'src-first','Story'),
        video_membership=CategoryStore('video').membership(db,'src-first','Movie/01.mp4'),
        progress=progress.read(db,'src-first','Story/01','folder','b'*64),
        playlists=playlists.listing(db,0,100),playlist_membership=playlists.membership(db,'one'),
        artist_assets=[value.model_dump(mode='json') for value in list_artist_profile_assets_from_conn(db,'author_a')])


@pytest.mark.parametrize('factory',[sqlite3.Row,'dict'])
def test_combined_essential_all_owners_preserve_records_and_snapshot_readers(combined_essential_source,tmp_path,factory):
    from database_connections import _row_factory
    case = combined_essential_source; original = case.source.read_bytes()
    with closing(sqlite3.connect(case.source)) as db:
        before = publication_state(db)
        objects = {(row[0],row[1]) for row in db.execute('SELECT type,name FROM sqlite_master WHERE sql IS NOT NULL')}
        db.row_factory = _row_factory if factory=='dict' else sqlite3.Row
        expected = combined_essential_projection(db)
    importers = combined_essential_importers()
    prepared = migrations.prepare_essential_candidate(case.source,tmp_path/'combined',catalog_importers=importers)
    assert prepared.imported_items == COMBINED_ESSENTIAL_COUNTS and prepared.registered_libraries == 3
    restored = tmp_path/'restored.sqlite'; migrations.snapshot_user_database(prepared.candidate,restored)
    with closing(sqlite3.connect(prepared.candidate)) as candidate, closing(sqlite3.connect(restored)) as db:
        assert publication_state(db) == publication_state(candidate)
        assert migrations._state(db,schema_objects=objects,table_names=set(before['table_rows'])) == before
        db.row_factory = _row_factory if factory=='dict' else sqlite3.Row
        assert combined_essential_projection(db) == expected
        owners = {row['owner']:row['n'] for row in db.execute('SELECT owner,count(*) AS n FROM kv_items GROUP BY owner')}
        assert owners == dict(files=6,danbooru=31,manga=37,video=19,language=8,twitter=5,youtube=2)
        assert db.execute('SELECT count(*) AS n FROM kv_assets').fetchone()['n'] == 6
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        assert youtube_metadata.preserved_library(db)['total'] == 2
        assert read_twitter_receipt(db)['payload']['record'] == twitter_receipt()
        assert read_twitter_receipt(db)['item_id'] != migrations.item_catalog.item_for_legacy(db,namespace='youtube.download',legacy_key='one')['id']
        assert db.execute('SELECT count(*) AS n FROM files_annotations').fetchone()['n'] == 6
        assert db.execute("SELECT seq FROM sqlite_sequence WHERE name='collections'").fetchone()['seq'] == 77
        assert not db.execute("SELECT 1 FROM kv_items WHERE owner IN ('music','ost')").fetchone()
    assert migrations.validate_essential_candidate(case.source,prepared.attempt_directory,catalog_importers=importers) == prepared
    assert case.source.read_bytes() == original


def test_combined_essential_canonical_order_and_exact_retry_keep_receipts(combined_essential_source,tmp_path):
    case = combined_essential_source; importers = combined_essential_importers()
    prepared = migrations.prepare_essential_candidate(case.source,tmp_path/'combined',catalog_importers=importers)
    before = {path.name:path.read_bytes() for path in prepared.attempt_directory.glob('*.json')}
    with closing(sqlite3.connect(prepared.candidate)) as db: expected = publication_state(db)
    assert migrations.prepare_essential_candidate(case.source,prepared.attempt_directory,catalog_importers=reversed(importers)) == prepared
    assert {path.name:path.read_bytes() for path in prepared.attempt_directory.glob('*.json')} == before
    with closing(sqlite3.connect(prepared.candidate)) as db: assert publication_state(db) == expected
    with pytest.raises(migrations.PreparationError):
        migrations.validate_essential_candidate(case.source,prepared.attempt_directory,catalog_importers=importers[:-1])
    assert {path.name:path.read_bytes() for path in prepared.attempt_directory.glob('*.json')} == before


@pytest.mark.parametrize('failure',['last-owner','legacy-write'])
def test_combined_essential_late_owner_failure_rolls_back_every_owner_and_recovers(combined_essential_source,tmp_path,failure):
    case = combined_essential_source; original = case.source.read_bytes()
    importers = combined_essential_importers(); last = importers[-1]
    def fail(db):
        count = last.import_records(db)
        if failure=='last-owner': raise InterruptedError('After final owner import')
        db.execute("UPDATE user_settings SET value='must roll back' WHERE key='profile_name'")
        return count
    broken = [*importers[:-1],migrations.CatalogImporter(last.name,last.version,fail)]
    with closing(sqlite3.connect(case.source)) as db:
        before = publication_state(db)
        objects = {(row[0],row[1]) for row in db.execute('SELECT type,name FROM sqlite_master WHERE sql IS NOT NULL')}
    attempt = tmp_path/'combined'
    with pytest.raises(migrations.PreparationError): migrations.prepare_essential_candidate(case.source,attempt,catalog_importers=broken)
    with closing(sqlite3.connect(attempt/'essential.sqlite')) as db:
        assert migrations._state(db,schema_objects=objects,table_names=set(before['table_rows'])) == before
        for table in ('kv_items','kv_libraries','kv_legacy_keys','kv_item_captures','kv_assets','kv_item_relations'):
            assert db.execute('SELECT count(*) FROM '+table).fetchone()[0] == 0
    assert not (attempt/'prepared.json').exists() and case.source.read_bytes() == original
    recovered = migrations.prepare_essential_candidate(case.source,attempt,catalog_importers=importers)
    assert recovered.imported_items == COMBINED_ESSENTIAL_COUNTS
    assert case.source.read_bytes() == original


@pytest.mark.parametrize('phase',['snapshot','prepared'])
def test_combined_essential_phase_interruption_resumes_without_duplicate_ids(combined_essential_source,tmp_path,monkeypatch,phase):
    case = combined_essential_source; importers = combined_essential_importers()
    original = migrations._record_phase; interrupted = False
    def record(attempt,name,**details):
        nonlocal interrupted
        original(attempt,name,**details)
        if name==phase and not interrupted:
            interrupted = True
            raise InterruptedError('After durable phase receipt')
    attempt = tmp_path/'combined'
    with monkeypatch.context() as scoped:
        scoped.setattr(migrations,'_record_phase',record)
        with pytest.raises(migrations.PreparationError): migrations.prepare_essential_candidate(case.source,attempt,catalog_importers=importers)
    prepared = migrations.prepare_essential_candidate(case.source,attempt,catalog_importers=importers)
    with closing(sqlite3.connect(prepared.candidate)) as db: expected = publication_state(db)
    assert migrations.prepare_essential_candidate(case.source,attempt,catalog_importers=importers) == prepared
    with closing(sqlite3.connect(prepared.candidate)) as db:
        assert publication_state(db) == expected
        assert db.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 108


def test_combined_essential_relocation_and_display_changes_keep_identities(combined_essential_source,tmp_path):
    case = combined_essential_source; importers = combined_essential_importers()
    first = migrations.prepare_essential_candidate(case.source,tmp_path/'first',catalog_importers=importers)
    with closing(sqlite3.connect(first.candidate)) as db: expected = files_essential_identities(db)
    with closing(sqlite3.connect(case.source)) as db:
        db.execute("UPDATE files_sources SET path=path||'-relocated',display_name=display_name||' changed'")
        db.execute("UPDATE collections SET name='Renamed collection' WHERE id=1")
        for table in ('manga_categories','video_categories'):
            db.execute('UPDATE '+table+" SET name='Renamed category',name_key='renamed category' WHERE rowid=(SELECT min(rowid) FROM "+table+')')
        db.commit()
    preserved = first.candidate.read_bytes()
    with pytest.raises(migrations.PreparationError,match='Source changed'):
        migrations.validate_essential_candidate(case.source,first.attempt_directory,catalog_importers=importers)
    assert first.candidate.read_bytes() == preserved
    second = migrations.prepare_essential_candidate(case.source,tmp_path/'second',catalog_importers=importers)
    with closing(sqlite3.connect(second.candidate)) as db: assert files_essential_identities(db) == expected


def test_combined_essential_changed_candidate_refuses_and_keeps_evidence(combined_essential_source,tmp_path):
    case = combined_essential_source; importers = combined_essential_importers()
    prepared = migrations.prepare_essential_candidate(case.source,tmp_path/'combined',catalog_importers=importers)
    with closing(sqlite3.connect(prepared.candidate)) as db:
        db.execute("UPDATE language_vocabulary SET english='external changed candidate'"); db.commit()
    modified = prepared.candidate.read_bytes()
    receipt = (prepared.attempt_directory/'prepared.json').read_bytes()
    with pytest.raises(migrations.PreparationError):
        migrations.validate_essential_candidate(case.source,prepared.attempt_directory,catalog_importers=importers)
    assert prepared.candidate.read_bytes() == modified
    assert (prepared.attempt_directory/'prepared.json').read_bytes() == receipt


def test_combined_essential_disabled_owners_keep_all_personal_records(combined_essential_source,tmp_path):
    case = combined_essential_source
    with closing(sqlite3.connect(case.source)) as db:
        db.execute("UPDATE suite_enabled_modules SET module='disabled-old-youtube' WHERE module='youtube'"); db.commit()
    prepared = migrations.prepare_essential_candidate(case.source,tmp_path/'combined',catalog_importers=combined_essential_importers())
    assert prepared.imported_items == COMBINED_ESSENTIAL_COUNTS
    with closing(sqlite3.connect(prepared.candidate)) as db:
        assert {row[0] for row in db.execute('SELECT module FROM suite_enabled_modules')} == {'disabled-old-youtube','unknown_future_owner'}
        assert db.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 108


def test_combined_essential_absent_owner_tables_are_not_invented(essential_source,tmp_path):
    prepared = migrations.prepare_essential_candidate(essential_source,tmp_path/'combined',catalog_importers=combined_essential_importers())
    assert prepared.imported_items == dict.fromkeys(COMBINED_ESSENTIAL_COUNTS,0)
    assert prepared.registered_libraries == 2
    with closing(sqlite3.connect(prepared.candidate)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not tables.intersection({'favorites','files_annotations','language_vocabulary','manga_categories','video_categories','twitter_downloads','youtube_downloads'})
        assert db.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM saved_jobs').fetchone()[0] == 1


def test_combined_essential_restored_snapshot_is_exact_in_fresh_readonly_process(combined_essential_source,tmp_path):
    case = combined_essential_source
    prepared = migrations.prepare_essential_candidate(case.source,tmp_path/'combined',catalog_importers=combined_essential_importers())
    restored = tmp_path/'restored.sqlite'; migrations.snapshot_user_database(prepared.candidate,restored)
    with closing(sqlite3.connect(restored)) as db: expected = publication_state(db)
    original = restored.read_bytes()
    code = r'''
import builtins,json,sqlite3,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
original=builtins.__import__
def guarded(name,globals=None,locals=None,fromlist=(),level=0):
    if level==0 and (name in ('config','database','lifecycle','suite_modules') or name.startswith('modules.')):
        raise AssertionError('Snapshot verification must not start an owner or runtime')
    return original(name,globals,locals,fromlist,level)
builtins.__import__=guarded
from services.data_migrations import _state
with sqlite3.connect(Path(sys.argv[2]).as_uri()+'?mode=ro',uri=True) as db:
    db.execute('PRAGMA query_only=ON')
    print(json.dumps(_state(db),sort_keys=True))
'''
    options = dict(capture_output=True,timeout=30)
    if os.name=='nt': options['creationflags'] = subprocess.CREATE_NO_WINDOW
    completed = subprocess.run([sys.executable,'-c',code,str(Path(__file__).resolve().parents[1]/'backend'),str(restored)],**options)
    assert completed.returncode == 0, completed.stderr.decode('utf-8','replace')
    assert json.loads(completed.stdout) == expected and restored.read_bytes() == original


def test_manga_metadata_import_is_independent_preserves_rows_and_rolls_back_late_failure():
    from modules.manga import metadata
    from services import item_catalog
    with sqlite3.connect(':memory:') as db:
        db.row_factory=sqlite3.Row;db.executescript(metadata.SCHEMA);item_catalog.ensure_catalog_schema(db)
        raw='{"tags":["한글"],"future":{"preserve":true}}'
        db.execute('INSERT INTO manga_metadata_overrides(source_id,path,kind,version,fields_json) VALUES(?,?,?,?,?)',('source','','series',1,raw));db.commit()
        before=[tuple(row) for row in db.execute('SELECT * FROM manga_metadata_overrides')]
        db.execute('BEGIN');assert metadata.import_catalog_records(db)==1;assert metadata.import_catalog_records(db)==1;db.commit()
        assert [tuple(row) for row in db.execute('SELECT * FROM manga_metadata_overrides')]==before
        count=db.execute('SELECT COUNT(*) FROM kv_items').fetchone()[0]
        db.execute('BEGIN')
        db.execute('INSERT INTO manga_metadata_overrides(source_id,path,kind,version,fields_json) VALUES(?,?,?,?,?)',('source','new','series',1,raw))
        db.execute('INSERT INTO manga_metadata_overrides(source_id,path,kind,version,fields_json) VALUES(?,?,?,?,?)',('source','../invalid','series',1,raw))
        with pytest.raises(ValueError):metadata.import_catalog_records(db)
        assert db.execute('SELECT COUNT(*) FROM kv_items').fetchone()[0]==count
        assert db.execute('SELECT COUNT(*) FROM manga_metadata_overrides').fetchone()[0]==3
        db.rollback()

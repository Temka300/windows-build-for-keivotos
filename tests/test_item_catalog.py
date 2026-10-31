"""Real SQLite identity, concurrency and transaction preservation contracts."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import hashlib
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from services import item_catalog as catalog


def asset_facts(identity='asset-one', **changes):
    return dict(asset_id=identity, media_type='video/webm', byte_size=42,
                hash_algorithm='sha256', content_hash='ab' * 32) | changes


def test_library_lookup_is_read_only_and_preserves_dictionary_factory(connection):
    identity = catalog.ensure_library(connection, namespace='files.source', legacy_key='source',
                                      kind='linked', display_name='Original')
    connection.row_factory = lambda cursor, row: dict(zip([column[0] for column in cursor.description], row))
    statements = []
    connection.set_trace_callback(statements.append)
    changes = connection.total_changes
    assert catalog.library_for_legacy(connection, namespace='files.source', legacy_key='source') == dict(id=identity, kind='linked')
    assert catalog.library_for_legacy(connection, namespace='files.source', legacy_key='unknown') is None
    assert connection.total_changes == changes
    assert all(statement.startswith('SELECT') for statement in statements)
    assert connection.execute('SELECT display_name FROM kv_libraries').fetchone() == {'display_name': 'Original'}


def test_asset_capture_retries_preserve_roles_and_observation_time(connection):
    identity = item(connection)
    assets = dict(media=asset_facts(), thumbnail=asset_facts('cover', media_type='image/png'))
    arguments = dict(item_id=identity, namespace='youtube.asset-observation', assets=assets,
                     payload=dict(source='source', path='relative/media.webm', missing=None))
    key = catalog.record_asset_capture(connection, **arguments)
    before = connection.execute('SELECT * FROM kv_item_captures').fetchall()
    assert catalog.record_asset_capture(connection, **arguments) == key
    assert connection.execute('SELECT * FROM kv_item_captures').fetchall() == before
    assert connection.execute('SELECT role,asset_id FROM kv_item_assets ORDER BY role').fetchall() == [('media', 'asset-one'), ('thumbnail', 'cover')]
    assert catalog.read_capture(connection, item_id=identity, namespace=arguments['namespace'], capture_key=key)['payload'] == arguments['payload']
    other = catalog.record_asset_capture(connection, **(arguments | dict(format_version=2)))
    assert other != key and connection.execute('SELECT count(*) FROM kv_item_captures').fetchone()[0] == 2


@pytest.mark.parametrize('failure', ['asset-conflict', 'role-conflict', 'invalid-facts', 'invalid-role', 'invalid-version', 'capture-trigger'])
def test_asset_capture_batch_rolls_back_every_new_row_on_failure(connection, failure):
    identity = item(connection)
    catalog.record_asset(connection, **asset_facts('preserved'))
    if failure == 'role-conflict':
        catalog.link_asset(connection, item_id=identity, asset_id='preserved', role='later')
    if failure == 'capture-trigger':
        connection.executescript("CREATE TRIGGER reject_capture BEFORE INSERT ON kv_item_captures BEGIN SELECT RAISE(ABORT,'retained failure'); END;")
    before = {table: connection.execute('SELECT * FROM ' + table).fetchall()
              for table in ('kv_assets', 'kv_item_assets', 'kv_item_captures')}
    later = asset_facts('new-later')
    if failure == 'asset-conflict':
        later = asset_facts('preserved', byte_size=99)
    elif failure == 'invalid-facts':
        later = dict(asset_id='incomplete')
    arguments = dict(item_id=identity, namespace='youtube.asset-observation',
        assets=dict(first=asset_facts('first'), **{'BadRole' if failure == 'invalid-role' else 'later': later}), payload=dict(kept=True))
    if failure == 'invalid-version':
        arguments['format_version'] = True
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        catalog.record_asset_capture(connection, **arguments)
    assert not connection.in_transaction
    assert before == {table: connection.execute('SELECT * FROM ' + table).fetchall() for table in before}


def test_asset_capture_respects_outer_transaction_and_savepoint(connection):
    identity = item(connection)
    connection.execute('CREATE TABLE personal(note TEXT)')
    connection.execute("INSERT INTO personal VALUES('pending')")
    arguments = dict(item_id=identity, namespace='youtube.asset-observation', assets=dict(media=asset_facts()), payload=dict(available=True))
    catalog.record_asset_capture(connection, **arguments)
    with pytest.raises(catalog.CatalogConflict):
        catalog.record_asset_capture(connection, **(arguments | dict(assets=dict(media=asset_facts('different')))))
    assert connection.in_transaction and connection.execute('SELECT note FROM personal').fetchone() == ('pending',)
    assert connection.execute('SELECT count(*) FROM kv_assets').fetchone()[0] == 1
    connection.rollback()
    assert connection.execute('SELECT * FROM personal').fetchall() == []
    for table in ('kv_assets', 'kv_item_assets', 'kv_item_captures'):
        assert connection.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0


def test_asset_capture_invalid_or_oversized_batches_write_nothing(connection):
    identity = item(connection)
    for assets in ([], {str(number): asset_facts(str(number)) for number in range(257)}):
        with pytest.raises(ValueError, match='batch'):
            catalog.record_asset_capture(connection, item_id=identity, namespace='observed', assets=assets, payload={})
    with pytest.raises(ValueError, match='too large'):
        catalog.record_asset_capture(connection, item_id=identity, namespace='observed', assets=dict(media=asset_facts()),
                                     payload={'text': 'x' * catalog.MAX_CAPTURE_BYTES})
    for table in ('kv_assets', 'kv_item_assets', 'kv_item_captures'):
        assert connection.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0


def test_parallel_asset_capture_exact_retries_retain_one_inventory(tmp_path):
    path = tmp_path / 'concurrent-assets.sqlite'
    with closing(sqlite3.connect(path)) as setup:
        catalog.ensure_catalog_schema(setup)
        identity = item(setup)
    def register(_):
        with closing(sqlite3.connect(path, timeout=10)) as connection:
            return catalog.record_asset_capture(connection, item_id=identity, namespace='observed',
                assets=dict(media=asset_facts()), payload=dict(source='same'))
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert len(set(pool.map(register, range(12)))) == 1
    with closing(sqlite3.connect(path)) as connection:
        for table in ('kv_assets', 'kv_item_assets', 'kv_item_captures'):
            assert connection.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 1


@pytest.fixture
def connection(tmp_path):
    with closing(sqlite3.connect(tmp_path / 'essential.sqlite')) as connection:
        catalog.ensure_catalog_schema(connection)
        yield connection


def item(connection, key='job-1', **changes):
    arguments = dict(namespace='youtube.job', legacy_key=key, owner='youtube', item_type='video')
    return catalog.ensure_item(connection, **(arguments | changes))


def test_schema_preserves_legacy_tables_and_database_version(connection):
    connection.executescript("CREATE TABLE original(id INTEGER PRIMARY KEY AUTOINCREMENT,note BLOB);"
                             "INSERT INTO original(note) VALUES(X'0001'); PRAGMA user_version=17;")
    before = connection.execute('SELECT * FROM original').fetchall()
    catalog.ensure_catalog_schema(connection)
    catalog.ensure_catalog_schema(connection)
    assert connection.execute('SELECT * FROM original').fetchall() == before
    assert connection.execute('PRAGMA user_version').fetchone()[0] == 17
    assert connection.execute('SELECT schema_version FROM kv_catalog_meta').fetchall() == [(2,)]


def test_schema_refuses_to_commit_pending_unrelated_work(connection):
    connection.execute('CREATE TABLE personal(note TEXT)')
    connection.execute("INSERT INTO personal VALUES('unsaved')")
    with pytest.raises(ValueError, match='pending transaction'):
        catalog.ensure_catalog_schema(connection)
    assert connection.in_transaction
    connection.rollback()
    assert connection.execute('SELECT * FROM personal').fetchall() == []


def test_future_schema_is_refused_without_downgrade(connection):
    connection.execute('UPDATE kv_catalog_meta SET schema_version=99')
    connection.commit()
    before = connection.total_changes
    with pytest.raises(ValueError, match='Unsupported'):
        catalog.ensure_catalog_schema(connection)
    assert connection.total_changes == before
    assert connection.execute('SELECT schema_version FROM kv_catalog_meta').fetchone()[0] == 99


def test_library_source_alias_is_stable_and_can_have_multiple_legacy_keys(connection):
    identity = catalog.ensure_library(connection, namespace='files.source', legacy_key='src-old',
                                      kind='linked', display_name='음악')
    assert catalog.ensure_library(connection, namespace='files.source', legacy_key='src-old',
                                  kind='linked', display_name='Renamed display') == identity
    assert catalog.ensure_library(connection, namespace='danbooru.root', legacy_key='root-old',
                                  kind='linked', display_name='음악', library_id=identity) == identity
    with pytest.raises(catalog.CatalogConflict):
        catalog.ensure_library(connection, namespace='files.source', legacy_key='src-old',
                               kind='managed', display_name='Other')
    assert connection.execute('SELECT count(*) FROM kv_libraries').fetchone()[0] == 1
    assert connection.execute('SELECT display_name FROM kv_libraries').fetchone()[0] == '음악'


def test_item_alias_retry_and_import_keep_identity_without_title_or_hash_matching(connection):
    identity = item(connection)
    assert item(connection) == identity
    assert item(connection, 'saved-manifest-id', namespace='youtube.manifest', item_id=identity) == identity
    assert item(connection, 'other-job') != identity
    assert item(connection, namespace='twitter.post', owner='twitter') != identity
    assert connection.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 3


@pytest.mark.parametrize('changes', [dict(owner='twitter'), dict(item_type='audio'), dict(item_id='other-item')])
def test_reused_alias_conflicts_without_orphan_item(connection, changes):
    identity = item(connection)
    with pytest.raises(catalog.CatalogConflict):
        item(connection, **changes)
    assert connection.execute('SELECT id FROM kv_items').fetchall() == [(identity,)]


def test_new_alias_cannot_reinterpret_existing_item(connection):
    identity = item(connection)
    with pytest.raises(catalog.CatalogConflict):
        item(connection, 'another-alias', owner='twitter', item_id=identity)
    assert connection.execute('SELECT legacy_key FROM kv_legacy_keys').fetchall() == [('job-1',)]


def test_catalog_writes_respect_outer_rollback_and_conflict_savepoint(connection):
    original = item(connection)
    connection.execute('BEGIN')
    pending = item(connection, 'pending')
    with pytest.raises(catalog.CatalogConflict):
        item(connection, owner='twitter')
    assert connection.in_transaction
    assert connection.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 2
    connection.rollback()
    assert connection.execute('SELECT id FROM kv_items').fetchall() == [(original,)]
    assert pending != original


def test_failed_commit_rolls_back_standalone_catalog_writes(connection):
    connection.executescript('CREATE TABLE preserved_parent(id INTEGER PRIMARY KEY);'
        'CREATE TABLE preserved_child(id INTEGER REFERENCES preserved_parent(id) DEFERRABLE INITIALLY DEFERRED);'
        'CREATE TRIGGER reject_catalog AFTER INSERT ON kv_items BEGIN '
        'INSERT INTO preserved_child VALUES(1); END;')
    with pytest.raises(sqlite3.IntegrityError):
        item(connection)
    assert not connection.in_transaction
    assert connection.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 0
    assert connection.execute('SELECT count(*) FROM kv_legacy_keys').fetchone()[0] == 0
    assert connection.execute('SELECT count(*) FROM preserved_child').fetchone()[0] == 0


def test_assets_share_between_items_but_do_not_merge_items_or_duplicate_hashes(connection):
    first, second = item(connection), item(connection, 'job-2')
    values = dict(media_type='video/webm', byte_size=123, hash_algorithm='sha256', content_hash='ab' * 32)
    catalog.record_asset(connection, asset_id='asset-1', **values)
    catalog.record_asset(connection, asset_id='asset-1', **values)
    catalog.record_asset(connection, asset_id='asset-2', **values)
    for identity in (first, second):
        catalog.link_asset(connection, item_id=identity, asset_id='asset-1', role='video')
        catalog.link_asset(connection, item_id=identity, asset_id='asset-1', role='video')
    with pytest.raises(catalog.CatalogConflict):
        catalog.record_asset(connection, asset_id='asset-1', **(values | dict(byte_size=124)))
    with pytest.raises(catalog.CatalogConflict):
        catalog.link_asset(connection, item_id=first, asset_id='asset-2', role='video')
    assert connection.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 2
    assert connection.execute('SELECT count(*) FROM kv_assets').fetchone()[0] == 2
    assert connection.execute('SELECT count(*) FROM kv_item_assets').fetchone()[0] == 2


def test_ordered_relations_are_idempotent_and_preserve_existing_members(connection):
    parent, one, two = item(connection), item(connection, 'one'), item(connection, 'two')
    catalog.relate_items(connection, source_item_id=parent, target_item_id=one, kind='episode', position=0)
    catalog.relate_items(connection, source_item_id=parent, target_item_id=two, kind='episode', position=1)
    catalog.relate_items(connection, source_item_id=parent, target_item_id=one, kind='episode', position=0)
    with pytest.raises(catalog.CatalogConflict):
        catalog.relate_items(connection, source_item_id=parent, target_item_id=two, kind='episode', position=0)
    assert connection.execute('SELECT target_item_id FROM kv_item_relations ORDER BY position').fetchall() == [(one,), (two,)]
    with pytest.raises(sqlite3.IntegrityError):
        catalog.relate_items(connection, source_item_id=parent, target_item_id=parent, kind='episode', position=2)
    with pytest.raises(sqlite3.IntegrityError):
        catalog.link_asset(connection, item_id=parent, asset_id='missing', role='cover')


def test_dict_row_factory_is_unchanged(connection):
    factory = lambda cursor, row: dict(zip([column[0] for column in cursor.description], row))
    connection.row_factory = factory
    first = item(connection)
    assert item(connection) == first
    assert connection.row_factory is factory
    assert connection.execute('SELECT id FROM kv_items').fetchone() == {'id': first}


def test_parallel_exact_retry_creates_one_identity(tmp_path):
    path = tmp_path / 'essential.sqlite'
    with closing(sqlite3.connect(path)) as setup:
        catalog.ensure_catalog_schema(setup)
    def create(_):
        with closing(sqlite3.connect(path, timeout=10)) as connection:
            return item(connection)
    with ThreadPoolExecutor(max_workers=4) as pool:
        identities = list(pool.map(create, range(12)))
    assert len(set(identities)) == 1
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute('SELECT count(*) FROM kv_items').fetchone()[0] == 1


@pytest.mark.parametrize('values', [dict(byte_size=-1), dict(byte_size=True), dict(hash_algorithm='sha256'),
                                  dict(hash_algorithm='md5', content_hash='bad'), dict(media_type='')])
def test_invalid_asset_facts_do_not_create_records(connection, values):
    with pytest.raises(ValueError):
        catalog.record_asset(connection, **(dict(asset_id='a', media_type='audio/flac') | values))
    assert connection.execute('SELECT count(*) FROM kv_assets').fetchone()[0] == 0


def test_version_one_upgrade_preserves_existing_records_and_schema(tmp_path):
    # Frozen v1 table shapes; missing unrelated tables are also filled additively.
    with closing(sqlite3.connect(tmp_path / 'version-one.sqlite')) as connection:
        connection.executescript('''
            CREATE TABLE kv_catalog_meta(id INTEGER PRIMARY KEY CHECK(id=1),schema_version INTEGER NOT NULL);
            INSERT INTO kv_catalog_meta VALUES(1,1);
            CREATE TABLE kv_items(id TEXT PRIMARY KEY,owner TEXT NOT NULL,item_type TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
            CREATE TABLE kv_legacy_keys(namespace TEXT NOT NULL,legacy_key TEXT NOT NULL,
                item_id TEXT NOT NULL REFERENCES kv_items(id),PRIMARY KEY(namespace,legacy_key));
            INSERT INTO kv_items VALUES('kept-id','youtube','saved-media','original-time');
            INSERT INTO kv_legacy_keys VALUES('youtube.download','job-old','kept-id');
            CREATE TABLE authored(id INTEGER PRIMARY KEY AUTOINCREMENT,note TEXT);
            INSERT INTO authored(note) VALUES('한국어');
            PRAGMA user_version=17;
            PRAGMA application_id=123;
        ''')
        original = {table: connection.execute('SELECT * FROM ' + table).fetchall()
                    for table in ('kv_items', 'kv_legacy_keys', 'authored', 'sqlite_sequence')}
        old_sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='kv_items'").fetchone()[0]
        catalog.ensure_catalog_schema(connection)
        assert {table: connection.execute('SELECT * FROM ' + table).fetchall() for table in original} == original
        assert connection.execute("SELECT sql FROM sqlite_master WHERE name='kv_items'").fetchone()[0] == old_sql
        assert connection.execute('SELECT schema_version FROM kv_catalog_meta').fetchone()[0] == 2
        assert connection.execute('SELECT count(*) FROM kv_item_captures').fetchone()[0] == 0
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 17
        assert connection.execute('PRAGMA application_id').fetchone()[0] == 123


def capture(connection, identity, key='original', **changes):
    return catalog.record_capture(connection, **(dict(item_id=identity, namespace='test.capture', capture_key=key,
        format_version=1, payload={'title': '한국어 🎵', 'missing': None, 'zero': 0, 'flag': False,
                                  'nested': [1, 1.25, {'text': 'line\nnext'}]}) | changes))


def test_capture_retry_is_canonical_and_changed_facts_keep_original(connection):
    identity = item(connection)
    digest = capture(connection, identity)
    first = catalog.read_capture(connection, item_id=identity, namespace='test.capture', capture_key='original')
    assert first['payload_sha256'] == digest
    assert first['payload']['missing'] is None and first['payload']['zero'] == 0
    assert first['payload']['flag'] is False
    assert capture(connection, identity) == digest
    assert capture(connection, identity, payload=dict(reversed(list(first['payload'].items())))) == digest
    for changes in (dict(payload={'title': 'changed'}), dict(format_version=2)):
        with pytest.raises(catalog.CatalogConflict):
            capture(connection, identity, **changes)
    assert catalog.read_capture(connection, item_id=identity, namespace='test.capture', capture_key='original') == first
    capture(connection, identity, key='new-capture', payload={'title': 'later facts'})
    assert connection.execute('SELECT count(*) FROM kv_item_captures').fetchone()[0] == 2


def test_capture_respects_foreign_keys_and_outer_rollback(connection):
    with pytest.raises(sqlite3.IntegrityError):
        capture(connection, 'missing-item')
    identity = item(connection)
    connection.execute('BEGIN')
    capture(connection, identity)
    with pytest.raises(catalog.CatalogConflict):
        capture(connection, identity, payload={'changed': True})
    assert connection.in_transaction
    connection.rollback()
    assert catalog.read_capture(connection, item_id=identity, namespace='test.capture', capture_key='original') is None


@pytest.mark.parametrize('payload', [[], {'bad': b'bytes'}, {1: 'numeric key'}, {'bad': (1, 2)},
    {'bad': float('nan')}, {'bad': float('inf')}, {'text': '한' * (catalog.MAX_CAPTURE_BYTES // 2)},
    {'many': [0] * 10001}])
def test_invalid_capture_payload_is_never_inserted(connection, payload):
    identity = item(connection)
    with pytest.raises(ValueError):
        capture(connection, identity, payload=payload)
    assert connection.execute('SELECT count(*) FROM kv_item_captures').fetchone()[0] == 0


def test_deep_and_cyclic_capture_objects_are_rejected(connection):
    identity = item(connection)
    cyclic = {}
    cyclic['self'] = cyclic
    nested = {}
    for _ in range(34):
        nested = {'child': nested}
    for payload in (cyclic, nested):
        with pytest.raises(ValueError, match='too complex'):
            capture(connection, identity, payload=payload)


@pytest.mark.parametrize('version', [0, -1, True, 1.0, 2**31])
def test_capture_format_requires_bounded_positive_integer(connection, version):
    with pytest.raises(ValueError, match='format version'):
        capture(connection, item(connection), format_version=version)


@pytest.mark.parametrize('replacement', ['{"changed":true}', '{"a":1, "a":1}', '{"a": 1}'])
def test_corrupt_or_noncanonical_stored_capture_is_refused(connection, replacement):
    identity = item(connection)
    capture(connection, identity)
    # Matching hash does not bless duplicate keys or a noncanonical representation.
    digest = 'invalid-digest' if replacement == '{"changed":true}' else hashlib.sha256(replacement.encode()).hexdigest()
    connection.execute('UPDATE kv_item_captures SET payload_json=?,payload_sha256=?', (replacement, digest))
    connection.commit()
    with pytest.raises(ValueError, match='payload changed'):
        catalog.read_capture(connection, item_id=identity, namespace='test.capture', capture_key='original')


def test_capture_and_alias_reads_keep_dict_row_factory_and_do_not_write(connection):
    identity = item(connection)
    capture(connection, identity)
    factory = lambda cursor, row: dict(zip([column[0] for column in cursor.description], row))
    connection.row_factory = factory
    statements = []
    connection.set_trace_callback(statements.append)
    before = connection.total_changes
    assert catalog.item_for_legacy(connection, namespace='youtube.job', legacy_key='job-1')['id'] == identity
    assert catalog.read_capture(connection, item_id=identity, namespace='test.capture', capture_key='original')['payload']['title'] == '한국어 🎵'
    assert catalog.item_for_legacy(connection, namespace='youtube.job', legacy_key='absent') is None
    assert connection.row_factory is factory and connection.total_changes == before
    assert all(statement.lstrip().upper().startswith('SELECT') for statement in statements)


def test_concurrent_capture_retry_preserves_one_record(tmp_path):
    path = tmp_path / 'capture.sqlite'
    with closing(sqlite3.connect(path)) as setup:
        catalog.ensure_catalog_schema(setup)
        identity = item(setup)
    def save(_):
        with closing(sqlite3.connect(path, timeout=10)) as connection:
            return capture(connection, identity)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert len(set(pool.map(save, range(12)))) == 1
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute('SELECT count(*) FROM kv_item_captures').fetchone()[0] == 1

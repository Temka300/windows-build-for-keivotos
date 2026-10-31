"""Coordinated profile activation and explicit index recovery.

No profile is selected or opened at import time. Legacy user databases are
read-only inputs; publication acknowledges all three stores together.
"""
from services.operation_diagnostics import observed
from contextlib import contextmanager, closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from database_connections import exclusive_database_access
from services import data_migrations as migration, catalog_index, item_manifest
from services.job_store_migration import initialize, prepare as prepare_jobs
from files_base import sources


@contextmanager
def connection(path, *, readonly=False):
    path=Path(path).absolute()
    db=sqlite3.connect(path.as_uri()+('?mode=ro' if readonly else '?mode=rw'),uri=True)
    try:
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        if readonly:db.execute('PRAGMA query_only=ON')
        yield db
        if not readonly:db.commit()
    finally:db.close()


def fingerprint(path):
    with connection(path,readonly=True) as db:
        if [tuple(r) for r in db.execute('PRAGMA quick_check')] != [('ok',)]:
            raise ValueError('Profile database failed integrity verification')
        return migration._state(db)


def _digest(value):
    return hashlib.sha256(item_manifest.encode(value)).hexdigest()


def _record(path,value):
    item_manifest._create(path,item_manifest.encode(value))


def _read(path):
    return json.loads(item_manifest._metadata_bytes(path),object_pairs_hook=item_manifest._unique)


def _guard(path):
    path=Path(path).absolute()
    if path.resolve()!=path or any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('Linked profile storage is unavailable')
    return path


def _copy(source,target):
    if target.exists():
        if fingerprint(source)!=fingerprint(target):raise ValueError('Preserved database copy conflicts')
    else:migration.snapshot_user_database(source,target)


def _has_table(path,table):
    if not Path(path).is_file():return False
    with connection(path,readonly=True) as db:
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone())


def _publish(candidate,target,previous,attempt):
    """Install only absent or exactly acknowledged inputs; retain every version."""
    candidate,target=_guard(candidate),_guard(target)
    expected=fingerprint(candidate)
    target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists():
        actual=fingerprint(target)
        if actual==expected:return
        if previous is None or actual!=previous:
            raise ValueError('Active database conflicts with prepared source')
        with connection(target) as db:
            if db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
                raise RuntimeError('Another process is using the profile database')
        saved=attempt/('previous-'+target.name)
        if saved.exists():
            # Rollback can retain a newer rejected generation beside the first
            # original. Every version gets its own name; none is replaced.
            saved=attempt/('previous-'+uuid4().hex+'-'+target.name)
        target.rename(saved)
        for suffix in ('-wal','-shm'):
            journal=Path(str(target)+suffix)
            if journal.exists():journal.rename(Path(str(saved)+suffix))
    os.link(candidate,target)
    item_manifest._flush_directory(target.parent)


def validate_active(home,essential,index,*,check_legacy=True,allow_missing_index=False):
    marker=_guard(Path(home)/'state/storage-active.json')
    if not marker.exists():return None
    receipt=_read(marker)
    if (set(receipt)!={'format','generation','essential','index','jobs','legacy'} or receipt['format']!=1
            or receipt['essential']!=str(_guard(essential)) or receipt['index']!=str(_guard(index))):
        raise ValueError('Unsupported active storage receipt')
    with connection(essential,readonly=True) as db:
        from services.item_catalog import SCHEMA_VERSION
        # The validator must not initialize or repair a damaged active database.
        row=db.execute('SELECT schema_version FROM kv_catalog_meta WHERE id=1').fetchone()
        if row is None or row[0]!=SCHEMA_VERSION:raise ValueError('Essential catalog schema is unavailable')
    if Path(index).exists() or not allow_missing_index:
        with connection(index,readonly=True) as db:catalog_index.validate_index(db)
    if check_legacy:
        for path,expected in receipt['legacy'].items():
            if Path(path).is_file() and fingerprint(path)!=expected:
                raise ValueError('Legacy source changed after activation; preserve both profiles for review')
    return receipt


def prepare_layout(home,registry,*,legacy_user,essential,index,jobs,legacy_indexes,enabled=(),
                   suite_schema='',asset_configuration=None,after_phase=lambda phase:None):
    """Prepare/resume and publish a complete generation before workers start."""
    home=_guard(home);essential=_guard(essential);index=_guard(index);jobs=_guard(jobs)
    state=_guard(home/'state');state.mkdir(parents=True,exist_ok=True)
    lock=_guard(state/'storage-preparation');lock.mkdir(exist_ok=True)
    with migration._attempt_lock(lock),exclusive_database_access():
        active=validate_active(home,essential,index)
        if active:return active
        original={}
        for path in dict.fromkeys([Path(legacy_user),*map(Path,legacy_indexes.values())]):
            if path.is_file():original[str(_guard(path))]=fingerprint(path)
        job_input=fingerprint(jobs) if jobs.is_file() else None
        selection=dict(legacy=original,jobs=job_input,essential=str(essential),index=str(index),
                       owners=[d.slug for d in registry],enabled=sorted(enabled))
        pending=state/'storage-pending.json'
        if pending.exists():
            recorded=_read(pending)
            if set(recorded)!={'attempt','selection'} or recorded['attempt']!=_digest(recorded['selection']):
                raise ValueError('Unsupported pending storage receipt')
            previous=recorded['selection']
            if any(previous[k]!=selection[k] for k in ('legacy','essential','index','owners','enabled')):
                raise ValueError('Pending storage inputs changed; recovery evidence retained')
            selection=previous
        attempt=_guard(lock/_digest(selection));attempt.mkdir(exist_ok=True)
        _record(pending,dict(attempt=attempt.name,selection=selection))
        _record(attempt/'intent.json',selection)
        job_input=selection['jobs']
        marker=home/'state/storage-active.json'
        if (attempt/'complete.json').exists():
            completed=_read(attempt/'complete.json')
            for name,target in [('essential',essential),('index',index),('jobs',jobs)]:
                _publish(attempt/(name+'.sqlite'),target,selection.get('jobs') if name=='jobs' else None,attempt)
            _record(marker,completed);after_phase('activated')
            return validate_active(home,essential,index)
        source=Path(legacy_user)
        if not source.is_file():
            source=attempt/'fresh.sqlite'
            if not source.exists():
                with closing(sqlite3.connect(source)) as db:
                    db.executescript(suite_schema)
                    sources.ensure_sources_schema(db)
        contributions=[d.transfer_provider() for d in registry if d.transfer_provider is not None]
        importers=tuple(i for c in contributions for i in c.catalog_importers)
        prepared=migration.prepare_essential_candidate(source,attempt/'essential-preparation',
                                                       catalog_importers=importers)
        candidate=attempt/'essential.sqlite'
        if not candidate.exists():migration.snapshot_user_database(prepared.candidate,candidate)
        after_phase('essential')
        job_candidate=attempt/'jobs.sqlite'
        if not job_candidate.exists():
            if jobs.is_file():migration.snapshot_user_database(jobs,job_candidate)
            else:
                with closing(sqlite3.connect(job_candidate)) as db:initialize(db)
        with connection(candidate) as receipts,connection(job_candidate) as operational:
            # No connection may retain a transaction when prepare() owns its commits.
            for contribution in contributions:
                table=contribution.operational_table
                if not table:continue
                old=Path(legacy_indexes.get(contribution.slug,candidate)) if contribution.operational_index else candidate
                if not _has_table(old,table):continue
                with connection(old,readonly=True) as legacy:
                    prepare_jobs(operational,legacy,receipts,owner=contribution.slug,table=table,
                                 preserve=contribution.preserve_operational)
        after_phase('jobs')
        # Initial activation copies observed tables; it never rescans media.
        # Rebuild() below deliberately selects the real rebuild callbacks.
        selected=set(enabled)
        for descriptor in registry:
            if descriptor.catalog_index_provider is None:continue
            owner=descriptor.catalog_index_provider()
            old=legacy_indexes.get(owner.slug)
            if old and any(_has_table(old,t) for t in owner.tables):selected.add(owner.slug)
        owners=catalog_index.owners_for(registry,selected)
        copy_owners=tuple(replace(o,rebuild=lambda build:None,observe=None) for o in owners)
        prepared_index=catalog_index.prepare_index(attempt/'index-preparation',
            essential_database=lambda:connection(candidate),owners=copy_owners,legacy_paths=legacy_indexes,
            sources=(),suite_home=home,lock_directory=state/'index-coordination')
        _copy(prepared_index.candidate,attempt/'index.sqlite')
        after_phase('index')
        if asset_configuration is not None:
            from services.asset_preservation import preserve
            with connection(candidate) as db:
                files=preserve(db,home,registry,asset_configuration)
            _record(attempt/'referenced-files.json',files)
            after_phase('assets')
        for path,expected in original.items():
            if fingerprint(path)!=expected:raise ValueError('Legacy source changed before activation')
        if jobs.is_file() and fingerprint(jobs)!=job_input:raise ValueError('Jobs changed before activation')
        # Preserve a complete acknowledgement before the first public path changes.
        completed=dict(format=1,generation=uuid4().hex,essential=str(essential),index=str(index),
                       jobs=str(jobs),legacy=original)
        _record(attempt/'complete.json',completed)
        for name,target in [('essential',essential),('index',index),('jobs',jobs)]:
            _publish(attempt/(name+'.sqlite'),target,job_input if name=='jobs' else None,attempt)
            after_phase('published-'+name)
        _record(marker,completed);after_phase('activated')
        return validate_active(home,essential,index)


@observed('suite', 'storage.activate')
def startup(configuration):
    """Called once before any schema publication, folder scan or owner worker."""
    from user_schema import USER_SCHEMA
    enabled=set()
    old=configuration.LEGACY_USER_DB_PATH
    if Path(old).is_file() and _has_table(old,'suite_enabled_modules'):
        with connection(old,readonly=True) as db:
            enabled={row[0] for row in db.execute('SELECT module FROM suite_enabled_modules')}
    paths={d.slug:(configuration.LEGACY_DATA_DB_PATH if d.slug=='danbooru'
                   else configuration.LEGACY_FILES_DB_PATH) for d in configuration.MODULE_REGISTRY
           if d.catalog_index_provider is not None}
    resume_install(configuration.SUITE_HOME)
    language=configuration.MODULE_REGISTRY.get('language')
    if language is not None:
        old_history=language.home/'revisions'
        if old_history.is_dir():
            destination=Path(configuration.SUITE_HOME)/'assets/language/revisions'
            for base,dirs,names in os.walk(old_history,followlinks=False):
                for name in [*dirs,*names]:_guard(Path(base)/name)
                for name in names:
                    source=_guard(Path(base)/name)
                    relative=source.relative_to(old_history).as_posix()
                    from storage_layout import portable_path
                    target=portable_path(destination,relative,(),must_exist=False)
                    target.parent.mkdir(parents=True,exist_ok=True)
                    observation=item_manifest.observe(old_history,item_manifest.asset(relative,asset_id='note-history'))
                    item_manifest.copy_observation(observation,target)
    restart=Path(configuration.SUITE_HOME)/'state/storage-restart-required.json'
    if restart.exists():
        raw=item_manifest._metadata_bytes(restart)
        history=restart.parent/'storage-install-history';history.mkdir(exist_ok=True)
        item_manifest._create(history/(hashlib.sha256(raw).hexdigest()+'.restart.json'),raw)
        restart.unlink()
    active=validate_active(configuration.SUITE_HOME,configuration.USER_DB_PATH,configuration.INDEX_DB_PATH,
                           allow_missing_index=True)
    if active and not Path(configuration.INDEX_DB_PATH).exists():
        restore_missing_index(configuration)
    return prepare_layout(configuration.SUITE_HOME,configuration.MODULE_REGISTRY,
        legacy_user=old,essential=configuration.USER_DB_PATH,index=configuration.INDEX_DB_PATH,
        jobs=configuration.JOBS_DB_PATH,legacy_indexes=paths,enabled=enabled,suite_schema=USER_SCHEMA,
        asset_configuration=configuration)


@observed('suite', 'index.rebuild')
def rebuild(configuration,*,cancelled=lambda:False):
    """Explicitly rebuild derived state; preserve personal/jobs and old indexes."""
    from database import get_user_db
    from maintenance import exclusive_tool_operation
    home=_guard(configuration.SUITE_HOME)
    with exclusive_tool_operation('rebuilding the library index',wait=False),exclusive_database_access():
        with get_user_db() as essential:
            registered=sources.list_sources(essential)
            enabled={row['module'] for row in essential.execute('SELECT module FROM suite_enabled_modules')}
        owners=catalog_index.owners_for(configuration.MODULE_REGISTRY,enabled)
        attempt=home/'recovery'/('index-'+uuid4().hex)
        attempt.mkdir(parents=True)
        result=catalog_index.prepare_index(attempt/'prepared',essential_database=get_user_db,
            owners=owners,legacy_paths={o.slug:configuration.INDEX_DB_PATH for o in owners},
            sources=registered,suite_home=home,lock_directory=home/'state/index-coordination',cancelled=cancelled)
        if cancelled():raise RuntimeError('Index rebuilding was cancelled; prepared files retained')
        previous=fingerprint(configuration.INDEX_DB_PATH) if Path(configuration.INDEX_DB_PATH).is_file() else None
        incoming=Path(configuration.INDEX_DB_PATH)
        if previous is None:incoming=result.candidate
        else:
            for owner in owners:
                merged=attempt/('merged-'+owner.slug+'.sqlite')
                catalog_index.merge_owner_snapshot(incoming,result.candidate,merged,owner)
                incoming=merged
        _publish(incoming,configuration.INDEX_DB_PATH,previous,attempt)
        return dict(status='rebuilt',generation=result.generation,recovery_path=str(attempt))


def _mutation_pointer(home):
    return _guard(Path(home)/'state/storage-install-pending.json')


def _finish_mutation(home,pointer,receipt):
    history=_guard(Path(home)/'state/storage-install-history');history.mkdir(exist_ok=True)
    raw=item_manifest._metadata_bytes(pointer)
    item_manifest._create(history/(hashlib.sha256(raw).hexdigest()+'.json'),raw)
    _record(Path(receipt['attempt'])/'installed.json',dict(format=1,targets=receipt['entries']))
    # This is a generated live pointer; its exact bytes already remain in history.
    pointer.unlink()
    item_manifest._flush_directory(pointer.parent)


@observed('suite', 'storage.install')
def install_databases(home,attempt,replacements,*,after_phase=lambda phase:None,resources=()):
    """Journal multi-store publication; a pending generation blocks runtime IO."""
    home=_guard(home);attempt=_guard(attempt)
    if not attempt.is_relative_to(home/'recovery'):raise ValueError('Installation evidence must stay in profile recovery')
    attempt.mkdir(parents=True,exist_ok=True)
    pointer=_mutation_pointer(home);pointer.parent.mkdir(parents=True,exist_ok=True)
    entries=[]
    for name,candidate,target in replacements:
        candidate,target=_guard(candidate),_guard(target)
        if not candidate.is_relative_to(attempt) or not target.is_relative_to(home):
            raise ValueError('Installation path is outside its authorized profile')
        previous=fingerprint(target) if target.is_file() else None
        if previous is not None:
            saved=attempt/('original-'+name+'.sqlite')
            _copy(target,saved)
        entries.append(dict(name=name,candidate=str(candidate),target=str(target),
                            previous=previous,prepared=fingerprint(candidate)))
    for name,candidate,target in resources:
        candidate,target=_guard(candidate),_guard(target)
        if not candidate.is_relative_to(attempt) or not _resource_allowed(home,target):
            raise ValueError('Resource installation is outside configured storage')
        previous=_resource_state(target) if target.exists() else None
        original=attempt/('original-resource-'+name)
        if previous is not None and not original.exists():_copy_resource(target,original)
        entries.append(dict(name=name,candidate=str(candidate),target=str(target),kind='resource',
                            previous=previous,prepared=_resource_state(candidate)))
    receipt=dict(format=1,attempt=str(attempt),entries=entries)
    _record(attempt/'install.json',receipt)
    _record(pointer,receipt)
    return resume_install(home,after_phase=after_phase)


@observed('suite', 'storage.resume')
def resume_install(home,*,after_phase=lambda phase:None):
    """Finish only an exact known old/new generation; never guess after conflicts."""
    home=_guard(home);pointer=_mutation_pointer(home)
    if not pointer.exists():return None
    receipt=_read(pointer)
    if set(receipt)!={'format','attempt','entries'} or receipt['format']!=1:
        raise ValueError('Unsupported pending database installation')
    attempt=_guard(receipt['attempt'])
    if not attempt.is_relative_to(home/'recovery') or _read(attempt/'install.json')!=receipt:
        raise ValueError('Database installation evidence changed')
    with exclusive_database_access():
        for entry in receipt['entries']:
            candidate,target=_guard(entry['candidate']),_guard(entry['target'])
            resource=entry.get('kind')=='resource'
            observe=_resource_state if resource else fingerprint
            allowed=_resource_allowed(home,target) if resource else target.is_relative_to(home)
            if not candidate.is_relative_to(attempt) or not allowed or observe(candidate)!=entry['prepared']:
                raise ValueError('Database installation candidate changed')
            actual=observe(target) if target.exists() else None
            if actual not in (None,entry['previous'],entry['prepared']):
                raise ValueError('Database installation target changed; retained for review')
        for entry in receipt['entries']:
            publish=_publish_resource if entry.get('kind')=='resource' else _publish
            publish(Path(entry['candidate']),Path(entry['target']),entry['previous'],attempt)
            after_phase('installed-'+entry['name'])
        _finish_mutation(home,pointer,receipt)
    return dict(status='installed',recovery_path=str(attempt))


@observed('suite', 'storage.rollback')
def rollback_install(home):
    """Explicit guarded rollback retains rejected new versions and all evidence."""
    home=_guard(home);pointer=_mutation_pointer(home)
    if not pointer.exists():raise ValueError('No pending installation to roll back')
    receipt=_read(pointer);attempt=_guard(receipt['attempt'])
    if not attempt.is_relative_to(home/'recovery') or _read(attempt/'install.json')!=receipt:
        raise ValueError('Database rollback evidence changed')
    with exclusive_database_access():
        for entry in receipt['entries']:
            target=_guard(entry['target'])
            resource=entry.get('kind')=='resource'
            observe=_resource_state if resource else fingerprint
            current=observe(target) if target.exists() else None
            if current not in (None,entry['previous'],entry['prepared']):
                raise ValueError('Changed target cannot be rolled back automatically')
            original=attempt/(('original-resource-'+entry['name']) if resource else ('original-'+entry['name']+'.sqlite'))
            if entry['previous'] is not None and observe(original)!=entry['previous']:
                raise ValueError('Original rollback snapshot changed')
        for entry in reversed(receipt['entries']):
            target=_guard(entry['target'])
            if entry['previous'] is not None:
                resource=entry.get('kind')=='resource'
                original=attempt/(('original-resource-'+entry['name']) if resource else ('original-'+entry['name']+'.sqlite'))
                (_publish_resource if resource else _publish)(original,target,entry['prepared'],attempt)
            elif target.exists():
                target.rename(attempt/('rejected-'+uuid4().hex+'-'+target.name))
        raw=item_manifest._metadata_bytes(pointer)
        history=_guard(home/'state/storage-install-history');history.mkdir(exist_ok=True)
        item_manifest._create(history/(hashlib.sha256(raw).hexdigest()+'.rolled-back.json'),raw)
        pointer.unlink()
    return dict(status='rolled_back',recovery_path=str(attempt))


def _resource_allowed(home,target):
    if target.is_relative_to(home):return True
    import config
    allowed={Path(config.RUNTIME_CONFIG_FILE).absolute()}
    from backup_bundle import COMPONENTS,_restore_kind
    allowed.update(Path(path).absolute() for key,(_,path) in COMPONENTS.items() if _restore_kind(key)!='sqlite')
    return target in allowed


def _resource_state(path):
    path=_guard(path)
    digest=hashlib.sha256();count=0
    if path.is_file():
        with path.open('rb') as stream:digest.update(hashlib.file_digest(stream,'sha256').digest())
        return dict(kind='file',sha256=digest.hexdigest(),files=1)
    if not path.is_dir():raise ValueError('Unsupported recovery resource')
    for base,dirs,files in os.walk(path,followlinks=False):
        for name in sorted([*dirs,*files]):
            current=_guard(Path(base)/name)
            relative=current.relative_to(path).as_posix()
            digest.update(item_manifest.encode([relative,'directory' if current.is_dir() else 'file']))
            if current.is_file():
                with current.open('rb') as stream:digest.update(hashlib.file_digest(stream,'sha256').digest())
                count+=1
        dirs.sort()
    return dict(kind='tree',sha256=digest.hexdigest(),files=count)


def _copy_resource(source,target):
    import shutil
    source,target=_guard(source),_guard(target)
    if source.is_dir():shutil.copytree(source,target)
    else:shutil.copy2(source,target)
    if _resource_state(source)!=_resource_state(target):raise ValueError('Recovery resource copy differs')
    paths=[target] if target.is_file() else [p for p in target.rglob('*') if p.is_file()]
    for path in paths:
        with path.open('r+b') as stream:stream.flush();os.fsync(stream.fileno())
    item_manifest._flush_directory(target.parent)


def _publish_resource(candidate,target,previous,attempt):
    candidate,target=_guard(candidate),_guard(target)
    expected=_resource_state(candidate)
    target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists() and _resource_state(target)==expected:return
    incoming=target.parent/('.keivotos-incoming-'+uuid4().hex)
    _copy_resource(candidate,incoming)
    if target.exists():
        if previous is None or _resource_state(target)!=previous:raise ValueError('Recovery resource changed')
        # Same-volume retained previous resource; never remove user originals.
        target.rename(target.parent/('.keivotos-previous-'+uuid4().hex))
    incoming.rename(target)
    item_manifest._flush_directory(target.parent)


@observed('suite', 'storage.prepare_restore')
def prepare_restore(configuration,replacements,attempt,*,operational_present):
    """Upgrade retained backup facts and prepare a matching derived generation."""
    home=Path(configuration.SUITE_HOME)
    attempt=Path(attempt);attempt.mkdir(parents=True,exist_ok=True)
    essentials=[entry for entry in replacements if entry[0]=='user_database']
    staged=[]
    if essentials:
        source=essentials[0][2]
        contributions=[d.transfer_provider() for d in configuration.MODULE_REGISTRY if d.transfer_provider]
        importers=tuple(i for c in contributions for i in c.catalog_importers)
        result=migration.prepare_essential_candidate(source,attempt/'essential-upgrade',catalog_importers=importers)
        essential=attempt/'essential.sqlite'
        migration.snapshot_user_database(result.candidate,essential)
        if not operational_present:
            # Older snapshots never acknowledged a jobs generation. Mark their
            # legacy queues as historical before any owner can initialize.
            from services.job_store_migration import RECEIPTS
            with connection(essential) as db:
                db.execute(RECEIPTS)
                for c in contributions:
                    if c.operational_table:
                        db.execute("INSERT OR IGNORE INTO kv_operational_migrations(owner,generation,version,history_lost) VALUES(?,?,1,1)",
                                   (c.slug,'restored-history-'+uuid4().hex))
        with connection(essential) as db:
            enabled={row[0] for row in db.execute('SELECT module FROM suite_enabled_modules')} if _has_table(essential,'suite_enabled_modules') else set()
            # Optional disabled owners' archived source facts also stay rebuildable.
            enabled.update(d.slug for d in configuration.MODULE_REGISTRY if d.catalog_index_provider and
                           catalog_index.latest_archive(db,d.slug))
        from backup_bundle import _index_backup_owner
        for key,live,restored in replacements:
            if Path(live)==Path(configuration.INDEX_DB_PATH):
                owner=_index_backup_owner(key,live)
                if owner is not None:enabled.add(owner.slug)
        owners=catalog_index.owners_for(configuration.MODULE_REGISTRY,enabled)
        paths={}
        for key,live,restored in replacements:
            if key=='user_database':continue
            if Path(restored).suffix=='.sqlite' and Path(live)==Path(configuration.INDEX_DB_PATH):
                with connection(restored,readonly=True) as db:
                    available={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for owner in owners:
                    if set(owner.tables)&available:paths[owner.slug]=restored
        # No media scan during a restore. Existing unique source facts are kept;
        # the separate explicit rebuild action reconstructs filesystem views.
        selected=tuple(replace(o,rebuild=lambda build:None,observe=None) for o in owners)
        rebuilt=catalog_index.prepare_index(attempt/'matching-index',essential_database=lambda:connection(essential),
            owners=selected,legacy_paths=paths,sources=(),suite_home=home,lock_directory=home/'state/index-coordination')
        staged.extend([('essential',essential,configuration.USER_DB_PATH),
                       ('index',rebuilt.candidate,configuration.INDEX_DB_PATH)])
    elif any(Path(live)==Path(configuration.INDEX_DB_PATH) for _,live,_ in replacements):
        incoming=Path(configuration.INDEX_DB_PATH)
        for key,live,restored in replacements:
            if Path(live)!=Path(configuration.INDEX_DB_PATH):continue
            from backup_bundle import _index_backup_owner
            owner=_index_backup_owner(key,live)
            if owner is None:raise ValueError('Index restore ownership is unavailable')
            merged=attempt/('merged-'+key+'.sqlite')
            catalog_index.merge_owner_snapshot(incoming,restored,merged,owner);incoming=merged
        staged.append(('index',incoming,configuration.INDEX_DB_PATH))
    resources=[]
    for key,live,restored in replacements:
        if key=='user_database' or Path(live)==Path(configuration.INDEX_DB_PATH):continue
        if key=='operational_database':
            candidate=attempt/'jobs.sqlite';migration.snapshot_user_database(restored,candidate)
            staged.append(('jobs',candidate,live))
        else:
            candidate=attempt/('resource-'+key)
            _copy_resource(restored,candidate);resources.append((key,candidate,live))
    return staged,resources


@observed('suite', 'index.recover_missing')
def restore_missing_index(configuration):
    home=_guard(configuration.SUITE_HOME)
    with exclusive_database_access():
        with connection(configuration.USER_DB_PATH) as db:
            enabled={r[0] for r in db.execute('SELECT module FROM suite_enabled_modules')} if _has_table(configuration.USER_DB_PATH,'suite_enabled_modules') else set()
            enabled.update(d.slug for d in configuration.MODULE_REGISTRY if d.catalog_index_provider and catalog_index.latest_archive(db,d.slug))
        owners=catalog_index.owners_for(configuration.MODULE_REGISTRY,enabled)
        selected=tuple(replace(o,rebuild=lambda build:None,observe=None) for o in owners)
        attempt=home/'recovery'/('missing-index-'+uuid4().hex);attempt.mkdir(parents=True)
        result=catalog_index.prepare_index(attempt/'prepared',
            essential_database=lambda:connection(configuration.USER_DB_PATH),
            owners=selected,legacy_paths={},sources=(),suite_home=home,lock_directory=home/'state/index-coordination')
        _publish(result.candidate,configuration.INDEX_DB_PATH,None,attempt)
    return dict(status='recovered',generation=result.generation)
